"""Read-only grant resolution for trusted services; no catalog backend or activation.

These values are internal contracts, not credentials and not client DTOs. A
reader must be bound to an authorized durable project source, verify complete
snapshots and their consent/policy dependencies, and reject stale generations.
There is deliberately no default reader, directory scanner or sample catalog.
Resolution neither lends a profile nor grants task execution. A later consumer
must recheck authority and source ordering before binding and dispatching.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from ampuly.layer4_tools.browser_account_status import LoginProbePolicy, _host
from ampuly.layer4_tools.browser_accounts import account_id_for_scope
from ampuly.layer4_tools.browser_sessions import ProfileScope, is_money_host


_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_GRANT = re.compile(r"bag_[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REVISION = (1 << 53) - 1
_REASONS = frozenset({
    "account_source_not_ready", "account_source_unavailable", "account_source_invalid",
    "account_source_changed", "account_ref_invalid", "account_grant_not_found",
    "account_grant_invalid", "account_grant_scope_mismatch", "account_grant_revoked",
    "account_grant_site_denied", "account_retention_unconfirmed",
    "account_policy_unavailable", "account_policy_mismatch",
})


class BrowserAccountSelectionRefused(RuntimeError):
    """Finite internal reason only; never interpolate source data or exceptions."""

    def __init__(self, reason: str) -> None:
        self.reason = reason if type(reason) is str and reason in _REASONS else "account_source_unavailable"
        super().__init__(self.reason)


def public_selection_error(error: BaseException) -> dict[str, str]:
    """Do not reveal whether an account exists in another customer's source."""
    return {"error": "browser_account_selection_unavailable", "message": "当前无法选择此账号。"}


@dataclass(frozen=True, repr=False)
class AuthorizedTaskScope:
    # The caller supplies this from the authorized task, never from a client scope.
    project_id: str
    customer_id: str
    environment_id: str
    task_id: str


@dataclass(frozen=True, repr=False)
class GrantSourceStamp:
    project_id: str
    source_id: str
    source_epoch: str
    source_revision: int
    content_digest: str
    schema_version: int = 1


@dataclass(frozen=True, repr=False)
class BrowserAccountGrant:
    grant_id: str
    grant_revision: int
    project_id: str
    customer_id: str
    environment_id: str
    identity_site_host: str
    account_label: str
    account_id: str
    display_name: str
    allowed_hosts: tuple[str, ...]
    state: str
    retention_receipt_ref: str
    login_policy_ref: str
    login_policy_digest: str


@dataclass(frozen=True, repr=False)
class RetentionEvidence:
    # A trusted reader resolves this durable fact; this boolean is not user consent.
    receipt_ref: str
    account_id: str
    confirmed: bool
    source: GrantSourceStamp


@dataclass(frozen=True, repr=False)
class GrantPolicySnapshot:
    policy_ref: str
    policy: LoginProbePolicy
    source: GrantSourceStamp


@dataclass(frozen=True, repr=False)
class GrantSnapshot:
    source: GrantSourceStamp
    grant: BrowserAccountGrant
    retention: RetentionEvidence | None
    policy: GrantPolicySnapshot | None


@dataclass(frozen=True)
class ResolvedAccountSelection:
    task_scope: AuthorizedTaskScope = field(repr=False)
    scope: ProfileScope = field(repr=False)
    login_policy: LoginProbePolicy = field(repr=False)
    grant_id: str
    grant_revision: int
    source: GrantSourceStamp = field(repr=False)
    retention_source: GrantSourceStamp = field(repr=False)
    policy_source: GrantSourceStamp = field(repr=False)
    allowed_hosts: tuple[str, ...] = field(repr=False)


class BrowserAccountGrantReader(Protocol):
    """Trusted adapter contract; implementation and persistence are not supplied.

    read_grant returns one consistent, validated durable grant and its dependency
    versions, or raises a finite refusal. Missing != unavailable != malformed.
    assert_current must recheck the exact grant, consent, policy, source instance
    and all stamps; comparing only the catalog revision is insufficient. Neither
    method may scan browser profiles, initialize stores or acquire an account.
    """

    def read_grant(self, grant_id: str, *, task_scope: AuthorizedTaskScope,
                   expected_source: GrantSourceStamp | None = None) -> GrantSnapshot: ...

    def assert_current(self, snapshot: GrantSnapshot, *, task_scope: AuthorizedTaskScope) -> None: ...


def _refused(reason: str) -> None:
    raise BrowserAccountSelectionRefused(reason)


def _text(value: object, limit: int) -> bool:
    if type(value) is not str or not value or value.strip() != value:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    try:
        return len(value.encode("utf-8")) <= limit
    except UnicodeError:
        return False


def _reference(value: object) -> bool:
    return type(value) is str and _REF.fullmatch(value) is not None


def _digest(value: object) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _revision(value: object) -> bool:
    return type(value) is int and 1 <= value <= _MAX_REVISION


def _task(task: AuthorizedTaskScope) -> None:
    if type(task) is not AuthorizedTaskScope or not all(_text(value, 256) for value in (
        task.project_id, task.customer_id, task.environment_id, task.task_id,
    )):
        _refused("account_grant_scope_mismatch")


def _source(stamp: GrantSourceStamp, task: AuthorizedTaskScope) -> None:
    if (type(stamp) is not GrantSourceStamp or type(stamp.schema_version) is not int
            or stamp.schema_version != 1 or not _text(stamp.project_id, 256)
            or not _reference(stamp.source_id) or not _reference(stamp.source_epoch)
            or not _revision(stamp.source_revision) or not _digest(stamp.content_digest)):
        _refused("account_source_invalid")
    if stamp.project_id != task.project_id:
        _refused("account_grant_scope_mismatch")


def _site(host: object) -> bool:
    if type(host) is not str:
        return False
    try:
        return _host(host) == host and not is_money_host(host)
    except ValueError:
        return False


def resolve_account_selection(snapshot: GrantSnapshot, *,
                              task_scope: AuthorizedTaskScope) -> ResolvedAccountSelection:
    """Validate internal facts only. No I/O, side effects, login or permission grant.

    Source/consent authenticity, digest provenance, monotonic history and revoked
    tombstones must be established by the trusted reader. This pure function can
    check their shape and relationships, not prove persistence from a data object.
    """
    _task(task_scope)
    if type(snapshot) is not GrantSnapshot:
        _refused("account_source_invalid")
    _source(snapshot.source, task_scope)
    grant = snapshot.grant
    if (type(grant) is not BrowserAccountGrant or type(grant.grant_id) is not str
            or not _GRANT.fullmatch(grant.grant_id) or not _revision(grant.grant_revision)
            or not all(_text(value, 256) for value in (
                grant.project_id, grant.customer_id, grant.environment_id))
            or not _text(grant.account_label, 512) or not _text(grant.display_name, 512)
            or not _digest(grant.account_id) or type(grant.state) is not str
            or grant.state not in {"active", "revoked"}):
        _refused("account_grant_invalid")
    if (grant.project_id, grant.customer_id, grant.environment_id) != (
        task_scope.project_id, task_scope.customer_id, task_scope.environment_id,
    ):
        _refused("account_grant_scope_mismatch")
    if grant.state == "revoked":
        _refused("account_grant_revoked")
    if (not _site(grant.identity_site_host) or type(grant.allowed_hosts) is not tuple
            or not 1 <= len(grant.allowed_hosts) <= 32
            or not all(_site(host) for host in grant.allowed_hosts)
            or len(set(grant.allowed_hosts)) != len(grant.allowed_hosts)
            or grant.identity_site_host not in grant.allowed_hosts):
        _refused("account_grant_site_denied")
    scope = ProfileScope(grant.project_id, grant.customer_id, grant.environment_id,
                         grant.identity_site_host, grant.account_label, keep=True)
    if account_id_for_scope(scope) != grant.account_id:
        _refused("account_grant_invalid")
    if scope.storable():
        _refused("account_grant_site_denied")

    consent = snapshot.retention
    if (type(consent) is not RetentionEvidence or consent.confirmed is not True
            or not _reference(grant.retention_receipt_ref)
            or not _reference(consent.receipt_ref) or not _digest(consent.account_id)
            or consent.receipt_ref != grant.retention_receipt_ref
            or consent.account_id != grant.account_id):
        _refused("account_retention_unconfirmed")
    _source(consent.source, task_scope)
    policy_snapshot = snapshot.policy
    if policy_snapshot is None:
        _refused("account_policy_unavailable")
    if (type(policy_snapshot) is not GrantPolicySnapshot
            or not _reference(grant.login_policy_ref) or not _digest(grant.login_policy_digest)
            or not _reference(policy_snapshot.policy_ref)
            or policy_snapshot.policy_ref != grant.login_policy_ref
            or type(policy_snapshot.policy) is not LoginProbePolicy
            or policy_snapshot.policy.site_host != grant.identity_site_host
            or policy_snapshot.policy.digest() != grant.login_policy_digest):
        _refused("account_policy_mismatch")
    _source(policy_snapshot.source, task_scope)
    # One durable source cannot simultaneously have three different versions.
    versions: dict[str, GrantSourceStamp] = {}
    for stamp in (snapshot.source, consent.source, policy_snapshot.source):
        previous = versions.setdefault(stamp.source_id, stamp)
        if previous != stamp:
            _refused("account_source_changed")
    return ResolvedAccountSelection(task_scope, scope, policy_snapshot.policy,
        grant.grant_id, grant.grant_revision, snapshot.source, consent.source,
        policy_snapshot.source, grant.allowed_hosts)


def read_account_selection(reader: BrowserAccountGrantReader | None = None, *,
                           grant_id: str, task_scope: AuthorizedTaskScope,
                           expected_source: GrantSourceStamp | None = None) -> ResolvedAccountSelection:
    """One read plus one freshness check; no retries or default source fallback.

    Only a server-owned adapter may be supplied. The check is not an atomic
    dispatch fence: a consumer must revalidate before binding and use a formally
    ordered revocation/dispatch mechanism before enabling execution.
    """
    _task(task_scope)
    if type(grant_id) is not str or not _GRANT.fullmatch(grant_id):
        _refused("account_ref_invalid")
    if expected_source is not None:
        _source(expected_source, task_scope)
    if reader is None:
        _refused("account_source_not_ready")
    try:
        read = getattr(reader, "read_grant", None)
        check = getattr(reader, "assert_current", None)
        if not callable(read) or not callable(check):
            _refused("account_source_not_ready")
        snapshot = read(grant_id, task_scope=task_scope, expected_source=expected_source)
    except BrowserAccountSelectionRefused:
        raise
    except Exception:
        raise BrowserAccountSelectionRefused("account_source_unavailable") from None
    result = resolve_account_selection(snapshot, task_scope=task_scope)
    if result.grant_id != grant_id:
        _refused("account_grant_invalid")
    if expected_source is not None and result.source != expected_source:
        _refused("account_source_changed")
    try:
        current = check(snapshot, task_scope=task_scope)
    except BrowserAccountSelectionRefused:
        raise
    except Exception:
        raise BrowserAccountSelectionRefused("account_source_unavailable") from None
    if current is not None:
        _refused("account_source_changed")
    return result
