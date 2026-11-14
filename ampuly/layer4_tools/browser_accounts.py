"""Exclusive storage for operator-authorized account profiles.

The registry validates permission and ProfileScope before calling this leaf.
Nothing here grants permission, restores abandoned locks, or deletes profiles.
An interrupted write stays pending until a controlled verification supplies a
durable receipt; an abandoned owner requires a separate recovery procedure.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any


MAX_RECORD_BYTES = 8192
IDENTITY_FIELDS = ("project_id", "customer_id", "environment_id", "site_host", "account_label")


class BrowserAccountRefused(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"Browser account unavailable: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class AccountHandle:
    account_id: str
    profile_path: Path
    token: str = field(repr=False)
    binding_key: str
    _identity: tuple[str, ...] = field(repr=False)
    verification_only: bool = False


def _bounded(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 512:
        raise BrowserAccountRefused(reason)
    return value


def _identity(scope: Any) -> tuple[str, ...]:
    values = tuple(getattr(scope, name, None) for name in IDENTITY_FIELDS)
    if any(not isinstance(value, str) or len(value.encode("utf-8")) > 512 for value in values):
        raise BrowserAccountRefused("invalid_scope")
    return (*values[:3], values[3].casefold(), values[4])


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _base(identity: tuple[str, ...]) -> dict[str, Any]:
    return {"schema": 1, "account_id": hashlib.sha256(_encoded(identity)).hexdigest(),
            "identity": list(identity)}


def account_id_for_scope(scope: Any) -> str:
    """Compute the existing storage identity without accessing account files."""
    return _base(_identity(scope))["account_id"]


def _pending_ref(record: dict[str, Any]) -> str:
    # Includes the original random owner token without exposing that token.
    canonical = _encoded({key: record[key] for key in sorted(record)})
    return hashlib.sha256(b"browser-account-pending-v1\0" + canonical).hexdigest()


def _check_path(path: Path) -> None:
    """Inspect ancestors without resolving away a symlink or Windows junction."""
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise BrowserAccountRefused("link_refused")
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise BrowserAccountRefused("recovery_required")


def _directory(path: Path) -> None:
    _check_path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_path(path)
    if not path.is_dir():
        raise BrowserAccountRefused("recovery_required")


def _read(path: Path) -> dict[str, Any] | None:
    _check_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES or info.st_nlink != 1:
            raise BrowserAccountRefused("recovery_required")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            payload = stream.read(MAX_RECORD_BYTES + 1)
        _check_path(path)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise BrowserAccountRefused("recovery_required")
        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result = dict(pairs)
            if len(result) != len(pairs):
                raise ValueError("duplicate field")
            return result
        value = json.loads(payload, object_pairs_hook=unique)
        if len(payload) > MAX_RECORD_BYTES or not isinstance(value, dict):
            raise ValueError("invalid record")
        return value
    except (ValueError, UnicodeError, OSError) as exc:
        raise BrowserAccountRefused("recovery_required") from exc
    finally:
        os.close(fd)


def _create(path: Path, record: dict[str, Any]) -> None:
    _check_path(path)
    payload = _encoded(record)
    if len(payload) > MAX_RECORD_BYTES:
        raise BrowserAccountRefused("recovery_required")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    # A failed write deliberately leaves a recovery-required file, never a free account.
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _check_path(path)


def _validate(record: dict[str, Any] | None, base: dict[str, Any], extra: set[str] = frozenset()) -> None:
    if record is None or set(record) != set(base) | extra or type(record.get("schema")) is not int:
        raise BrowserAccountRefused("recovery_required")
    if any(record.get(key) != value for key, value in base.items()):
        raise BrowserAccountRefused("account_identity_mismatch")
    for key in extra:
        _bounded(record[key], "recovery_required")


def _serialized(operation):
    @wraps(operation)
    def call(self, *args, **kwargs):
        # Prevent two local close calls from straddling release and reacquisition.
        with self._mutex:
            try:
                return operation(self, *args, **kwargs)
            except OSError as exc:
                raise BrowserAccountRefused("recovery_required") from exc
    return call


class BrowserAccountStore:
    """Root is trusted configuration, normally AMPULY_HOME/browser/accounts."""

    def __init__(self, root: Path, *, native_lease_root: Path | None = None) -> None:
        self._native_lease_root = Path(os.path.abspath(native_lease_root)) if native_lease_root is not None else None
        if self._native_lease_root is not None:
            _check_path(self._native_lease_root)
        self._native_held: dict[str, tuple[Path, dict[str, Any]]] = {}
        self.root = Path(os.path.abspath(root))
        parts = tuple(part.casefold() for part in self.root.parts)
        if any(parts[index:index + 2] == ("browser", "profiles") for index in range(len(parts) - 1)):
            raise BrowserAccountRefused("unsafe_account_root")
        _check_path(self.root)
        self._held: dict[str, AccountHandle] = {}
        self._verification_refs: dict[str, str] = {}
        self._mutex = threading.RLock()

    def _location(self, identity: tuple[str, ...]) -> tuple[Path, dict[str, Any]]:
        base = _base(identity)
        directory = self.root / base["account_id"]
        _check_path(directory)
        return directory, base

    def _metadata(self, directory: Path, base: dict[str, Any], *, create: bool = False) -> None:
        path = directory / "identity.json"
        record = _read(path)
        if record is None and create:
            _check_path(directory / "profile")
            if (directory / "profile").exists():
                raise BrowserAccountRefused("recovery_required")
            try:
                _create(path, base)
            except FileExistsError:
                pass
            record = _read(path)
        _validate(record, base)

    def _marker(self, directory: Path, base: dict[str, Any], name: str) -> dict[str, Any] | None:
        record = _read(directory / name)
        if record is not None:
            extra = {"token", "binding_key", "action_id"} if name == "pending.json" else set()
            _validate(record, base, extra)
        return record

    @_serialized
    def inspect(self, scope: Any) -> dict[str, Any]:
        """Observe one already-authorized scope, without acquiring or changing it.

        ``present`` means a validated identity record, not an executable account.
        Owner presence says nothing about process liveness. Two bounded reads
        reject observed changes; this observation is neither a lock nor permission
        and may become stale immediately after return. No profile content is read.
        """
        directory, base = self._location(_identity(scope))
        files = tuple(directory / name for name in ("identity.json", "owner.json", "pending.json", "revoked.json"))
        paths = (self.root, directory, *files, directory / "profile")

        def stamp(path: Path) -> tuple[Any, ...] | None:
            _check_path(path)
            try:
                info = path.lstat()
            except FileNotFoundError:
                return None
            if path in (self.root, directory, directory / "profile"):
                if not stat.S_ISDIR(info.st_mode):
                    raise BrowserAccountRefused("recovery_required")
                return info.st_dev, info.st_ino, info.st_mode
            return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        def snapshot() -> tuple[Any, ...]:
            before = tuple(stamp(path) for path in paths)
            records = tuple(_read(path) for path in files)
            after = tuple(stamp(path) for path in paths)
            if before != after:
                raise BrowserAccountRefused("account_observation_changed")
            return records, after

        first = snapshot()
        if first != snapshot():
            raise BrowserAccountRefused("account_observation_changed")
        (identity, owner, pending, revoked), stamps = first
        profile_present = stamps[-1] is not None
        if identity is None:
            if any(record is not None for record in (owner, pending, revoked)) or profile_present:
                raise BrowserAccountRefused("recovery_required")
        else:
            _validate(identity, base)
        for record, extra in ((owner, {"token", "binding_key"}),
                              (pending, {"token", "binding_key", "action_id"}), (revoked, set())):
            if record is not None:
                _validate(record, base, extra)
        return {"account_id": base["account_id"], "present": identity is not None,
                "profile_present": profile_present, "owner_present": owner is not None,
                "pending_present": pending is not None, "revoked": revoked is not None}

    @_serialized
    def acquire(self, scope: Any, binding_key: str) -> AccountHandle:
        return self._acquire(scope, binding_key, verification_only=False)

    @_serialized
    def acquire_for_verification(self, scope: Any, binding_key: str) -> AccountHandle:
        """Borrow only a closed account's pending result; never recover an owner."""
        return self._acquire(scope, binding_key, verification_only=True)

    def _acquire(self, scope: Any, binding_key: str, *, verification_only: bool) -> AccountHandle:
        identity = _identity(scope)
        _bounded(binding_key, "invalid_binding")
        directory, base = self._location(identity)
        if not verification_only:
            _directory(directory)
        # Read-only preflight avoids manufacturing an abandoned owner for an
        # already denied account. The same markers are checked under ownership.
        existing = _read(directory / "identity.json")
        if existing is not None:
            _validate(existing, base)
        if self._marker(directory, base, "revoked.json") is not None:
            raise BrowserAccountRefused("account_revoked")
        pending = self._marker(directory, base, "pending.json")
        if verification_only and pending is None:
            raise BrowserAccountRefused("no_pending_verification")
        if not verification_only and pending is not None:
            raise BrowserAccountRefused("recovery_required")
        original_ref = _pending_ref(pending) if pending is not None else ""
        token = secrets.token_hex(32)
        lock = directory / "owner.json"
        record = {**base, "token": token, "binding_key": binding_key}
        try:
            _create(lock, record)
        except FileExistsError:
            _validate(_read(lock), base, {"token", "binding_key"})
            raise BrowserAccountRefused("account_busy") from None
        self._metadata(directory, base, create=not verification_only)
        # Keep the exclusive owner file after any failure; never silently recover.
        if self._marker(directory, base, "revoked.json") is not None:
            raise BrowserAccountRefused("account_revoked")
        pending = self._marker(directory, base, "pending.json")
        if verification_only and (pending is None or _pending_ref(pending) != original_ref):
            raise BrowserAccountRefused("stale_pending_ref")
        if not verification_only and pending is not None:
            raise BrowserAccountRefused("recovery_required")
        profile = directory / "profile"
        if verification_only:
            _check_path(profile)
            if not profile.is_dir():
                raise BrowserAccountRefused("recovery_required")
        else:
            _directory(profile)
        handle = AccountHandle(base["account_id"], profile, token, binding_key, identity, verification_only)
        self._held[handle.account_id] = handle
        if verification_only:
            self._verification_refs[handle.account_id] = original_ref
        self.assert_current(handle)
        return handle

    def _owned(self, handle: AccountHandle) -> tuple[Path, dict[str, Any]]:
        if not isinstance(handle, AccountHandle) or self._held.get(handle.account_id) != handle:
            raise BrowserAccountRefused("account_handle_invalid")
        directory, base = self._location(handle._identity)
        if handle.account_id != base["account_id"] or handle.profile_path != directory / "profile":
            raise BrowserAccountRefused("account_handle_invalid")
        _check_path(handle.profile_path)
        self._metadata(directory, base)
        record = _read(directory / "owner.json")
        _validate(record, base, {"token", "binding_key"})
        if record["token"] != handle.token or record["binding_key"] != handle.binding_key:
            raise BrowserAccountRefused("account_handle_invalid")
        return directory, base

    @_serialized
    def assert_current(self, handle: AccountHandle) -> None:
        directory, base = self._owned(handle)
        if self._marker(directory, base, "revoked.json") is not None:
            raise BrowserAccountRefused("account_revoked")
        self._assert_native_owner(handle)

    @_serialized
    def confirm_closed(self, handle: AccountHandle) -> None:
        """Caller must first confirm actual runtime closure. Keep profile and markers."""
        directory, _ = self._owned(handle)
        self._assert_native_owner(handle)
        native = self._native_held.get(handle.account_id)
        if native is not None:
            native[0].unlink()
            del self._native_held[handle.account_id]
        (directory / "owner.json").unlink()
        del self._held[handle.account_id]
        self._verification_refs.pop(handle.account_id, None)

    @_serialized
    def mark_unknown(self, handle: AccountHandle, action_id: str) -> None:
        """Persist intent before input; a pending action permits only controlled reads."""
        _bounded(action_id, "invalid_action")
        self.assert_current(handle)
        if handle.verification_only:
            raise BrowserAccountRefused("account_verification_only")
        directory, base = self._owned(handle)
        record = {**base, "token": handle.token, "binding_key": handle.binding_key, "action_id": action_id}
        try:
            _create(directory / "pending.json", record)
        except FileExistsError:
            if self._marker(directory, base, "pending.json") != record:
                raise BrowserAccountRefused("account_write_pending") from None

    @_serialized
    def resolve_unknown(self, handle: AccountHandle, action_id: str, evidence_ref: str) -> None:
        """Only the trusted gateway calls this after recording a conclusive receipt."""
        _bounded(action_id, "invalid_action")
        _bounded(evidence_ref, "evidence_required")
        directory, base = self._owned(handle)
        if handle.verification_only:
            raise BrowserAccountRefused("account_verification_only")
        record = self._marker(directory, base, "pending.json")
        expected = {**base, "token": handle.token, "binding_key": handle.binding_key, "action_id": action_id}
        if record != expected:
            raise BrowserAccountRefused("account_write_pending")
        (directory / "pending.json").unlink()

    @_serialized
    def pending_view(self, handle: AccountHandle) -> dict[str, str]:
        """Project the original intent, without owner tokens or local paths."""
        self.assert_current(handle)
        directory, base = self._owned(handle)
        record = self._marker(directory, base, "pending.json")
        if record is None:
            raise BrowserAccountRefused("no_pending_verification")
        reference = _pending_ref(record)
        if handle.verification_only and self._verification_refs.get(handle.account_id) != reference:
            raise BrowserAccountRefused("stale_pending_ref")
        return {"binding_key": record["binding_key"], "action_id": record["action_id"], "pending_ref": reference}

    @_serialized
    def resolve_verified(self, handle: AccountHandle, pending_ref: str, outcome: str, evidence_ref: str) -> None:
        """Trusted service records an already verified result; this grants no write permission.

        The caller owns the durable Gateway/human verification receipt. This
        store checks its reference and the exact pending intent, not the remote
        outcome. The verification handle stays read-only after resolution.
        """
        self.assert_current(handle)
        if handle.verification_only is not True:
            raise BrowserAccountRefused("verification_handle_required")
        _bounded(pending_ref, "stale_pending_ref")
        _bounded(evidence_ref, "evidence_required")
        if outcome not in ("succeeded", "failed_before_effect"):
            raise BrowserAccountRefused("invalid_verification_outcome")
        view = self.pending_view(handle)
        if pending_ref != view["pending_ref"]:
            raise BrowserAccountRefused("stale_pending_ref")
        directory, _ = self._owned(handle)
        (directory / "pending.json").unlink()

    @_serialized
    def revoke(self, scope: Any) -> None:
        """Write a monotonic denial; never close a browser or delete account data."""
        directory, base = self._location(_identity(scope))
        _directory(directory)
        self._metadata(directory, base, create=True)
        try:
            _create(directory / "revoked.json", base)
        except FileExistsError:
            self._marker(directory, base, "revoked.json")


    @staticmethod
    def _native_locator(value: str, reason: str) -> str:
        if (type(value) is not str or not value or value != value.strip()
                or len(value.encode("utf-8")) > 256 or any(ord(c) < 32 for c in value)):
            raise BrowserAccountRefused(reason)
        return value

    def _native_binding(self, handle: AccountHandle) -> dict[str, Any]:
        directory, base = self._owned(handle)
        record = _read(directory / "native-profile.json")
        if record is None:
            raise BrowserAccountRefused("native_profile_mapping_required")
        _validate(record, base, {"host_id", "native_profile_id", "verification_ref"})
        self._native_locator(record["host_id"], "native_profile_mapping_invalid")
        self._native_locator(record["native_profile_id"], "native_profile_mapping_invalid")
        if record["native_profile_id"].casefold() == "default":
            raise BrowserAccountRefused("native_profile_mapping_invalid")
        return record

    @_serialized
    def bind_native_profile(self, handle: AccountHandle, *, host_id: str,
                            native_profile_id: str, verification_ref: str) -> dict[str, str]:
        """Trusted host records a previously verified exact mapping, never creates a Native profile.

        Account authorization remains the caller's responsibility. This immutable
        attachment is not evidence that a profile exists or a runtime can enforce
        policy. Rebinding needs a separate controlled migration, not an overwrite.
        """
        self.assert_current(handle)
        directory, base = self._owned(handle)
        if handle.verification_only or self._marker(directory, base, "pending.json") is not None:
            raise BrowserAccountRefused("account_verification_only")
        self._native_locator(host_id, "invalid_native_host")
        self._native_locator(native_profile_id, "invalid_native_profile")
        _bounded(verification_ref, "evidence_required")
        if native_profile_id.casefold() == "default":
            raise BrowserAccountRefused("invalid_native_profile")
        record = {**base, "host_id": host_id, "native_profile_id": native_profile_id,
                  "verification_ref": verification_ref}
        try:
            _create(directory / "native-profile.json", record)
        except FileExistsError:
            if self._native_binding(handle) != record:
                raise BrowserAccountRefused("native_profile_mapping_immutable") from None
        return self.native_profile_binding(handle)

    @_serialized
    def native_profile_binding(self, handle: AccountHandle) -> dict[str, str]:
        """Read only this owned account's mapping, without enumerating Native profiles."""
        self.assert_current(handle)
        record = self._native_binding(handle)
        return {key: record[key] for key in ("host_id", "native_profile_id", "verification_ref")}

    def _assert_native_owner(self, handle: AccountHandle) -> None:
        held = self._native_held.get(handle.account_id)
        if held is None:
            return
        path, expected = held
        if _read(path) != expected:
            raise BrowserAccountRefused("native_profile_lease_changed")
        mapping = self._native_binding(handle)
        if any(mapping[key] != expected[key] for key in ("host_id", "native_profile_id", "verification_ref")):
            raise BrowserAccountRefused("native_profile_mapping_changed")

    @_serialized
    def acquire_native_profile(self, handle: AccountHandle) -> dict[str, str]:
        """Reserve host+profile before Native creation; unknown/abandoned owners stay held.

        Every account store on the host must receive the same trusted lease root.
        There is no per-project default and no PID/timeout recovery. Only the
        existing confirm_closed path releases this reservation after real closure.
        This leaf does not select a backend or relax runtime policy admission.
        """
        self.assert_current(handle)
        if self._native_lease_root is None:
            raise BrowserAccountRefused("native_profile_lease_root_required")
        mapping = self._native_binding(handle)
        lease_key = hashlib.sha256(_encoded([mapping["host_id"], mapping["native_profile_id"]])).hexdigest()
        if handle.account_id not in self._native_held:
            _directory(self._native_lease_root)
            path = self._native_lease_root / (lease_key + ".json")
            _, base = self._owned(handle)
            record = {**base, **mapping, "token": handle.token, "binding_key": handle.binding_key}
            try:
                _create(path, record)
            except FileExistsError:
                # Never adopt another account/process, even after its process died.
                raise BrowserAccountRefused("native_profile_busy") from None
            self._native_held[handle.account_id] = (path, record)
        self._assert_native_owner(handle)
        return {"host_id": mapping["host_id"], "native_profile_id": mapping["native_profile_id"],
                "lease_key": lease_key}
