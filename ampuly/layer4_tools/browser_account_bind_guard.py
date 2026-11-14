"""Authorize bind from a selection record + selection dispatch_guard (B06-C2/C4).

Pure local path: revalidate SelectionRecordFact against current grants, build a
BindAuthorizationFact, optionally attach via BrowserSessionRegistry.bind_account_profile.
C4: install_selection_agent_dispatch_guard wires selection_dispatch_guard into the
process-local ``_AGENT_DISPATCH_GUARD`` ContextVar (no live Playwright, no Gateway
Admission). Never writes dispatch_links or advances catalog source_revision.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

from ampuly.database.browser_account_grant_store import (
    _ReadyProjectGrantBinding,
    reader_from_ready_store,
)
from ampuly.database.browser_account_selection_records import (
    SelectionRecordFact,
    read_selection_record,
)
from ampuly.layer4_tools.browser_account_selection import (
    BrowserAccountSelectionRefused,
    RetentionEvidence,
    public_selection_error,
)
from ampuly.layer4_tools.browser_account_status import LoginProbePolicy
from ampuly.layer4_tools.browser_sessions import (
    BrowserBinding,
    ProfileScope,
)

BIND_AUTHORIZATION_KIND = "bind_authorization_v1"

public_bind_guard_error = public_selection_error


@dataclass(frozen=True, repr=False)
class BindAuthorizationFact:
    """Immutable bind authorization derived from a selection record.

    Not an execution token: no lease, fence, attempt_id, or can_execute bit.
    Applying it only records an in-memory profile choice; it does not prove the
    website login is valid and does not enable page input by itself.
    """

    authorization_kind: str
    selection_id: str
    selection_revision: int
    binding_key: str
    task_scope: object
    grant_id: str
    grant_revision: int
    account_id: str
    profile_scope: ProfileScope
    login_policy: LoginProbePolicy
    source: object
    retention_source: object
    policy_source: object
    allowed_hosts: tuple[str, ...]


def _reader(store):
    try:
        entry = getattr(store, "browser_account_grant_reader", None)
        if callable(entry):
            return entry()
        return reader_from_ready_store(store)
    except BrowserAccountSelectionRefused:
        raise
    except Exception:
        raise BrowserAccountSelectionRefused("account_source_unavailable") from None


def _load_fact(store, selection_id: str) -> SelectionRecordFact:
    if type(selection_id) is not str or not selection_id:
        raise BrowserAccountSelectionRefused("account_source_invalid")
    try:
        binding = _ReadyProjectGrantBinding.capture(store)
        with binding.read_transaction() as conn:
            return read_selection_record(conn, selection_id=selection_id)
    except BrowserAccountSelectionRefused:
        raise
    except Exception:
        raise BrowserAccountSelectionRefused("account_source_unavailable") from None


def _binding_matches(binding: BrowserBinding, task_scope) -> bool:
    if type(binding) is not BrowserBinding:
        return False
    return (
        binding.project_id == task_scope.project_id
        and binding.customer_id == task_scope.customer_id
        and binding.environment_id == task_scope.environment_id
        and binding.task_id == task_scope.task_id
    )


def _authorize_from_fact(
    store,
    fact: SelectionRecordFact,
    *,
    binding: BrowserBinding,
    keep: bool,
) -> BindAuthorizationFact:
    if type(fact) is not SelectionRecordFact:
        raise BrowserAccountSelectionRefused("account_source_invalid")
    if type(keep) is not bool:
        raise BrowserAccountSelectionRefused("account_source_invalid")
    if not _binding_matches(binding, fact.task_scope):
        raise BrowserAccountSelectionRefused("account_grant_scope_mismatch")

    reader = _reader(store)
    try:
        snapshot = reader.read_grant(
            fact.grant_id,
            task_scope=fact.task_scope,
        )
    except BrowserAccountSelectionRefused:
        raise
    except Exception:
        raise BrowserAccountSelectionRefused("account_source_unavailable") from None

    consent = getattr(snapshot, "retention", None)
    grant = getattr(snapshot, "grant", None)
    policy_snap = getattr(snapshot, "policy", None)
    if grant is None or policy_snap is None:
        raise BrowserAccountSelectionRefused("account_source_invalid")
    if type(consent) is not RetentionEvidence or consent.confirmed is not True:
        raise BrowserAccountSelectionRefused("account_retention_unconfirmed")

    policy = getattr(policy_snap, "policy", None)
    if type(policy) is not LoginProbePolicy:
        raise BrowserAccountSelectionRefused("account_source_invalid")

    if (
        grant.grant_id != fact.grant_id
        or grant.grant_revision != fact.grant_revision
        or grant.account_id != fact.account_id
        or snapshot.source != fact.source
        or consent.source != fact.retention_source
        or policy_snap.source != fact.policy_source
    ):
        raise BrowserAccountSelectionRefused("account_source_changed")

    if grant.state != "active":
        raise BrowserAccountSelectionRefused("account_grant_revoked")

    profile = ProfileScope(
        project_id=fact.task_scope.project_id,
        customer_id=fact.task_scope.customer_id,
        environment_id=fact.task_scope.environment_id,
        site_host=grant.identity_site_host,
        account_label=grant.account_label,
        keep=keep,
    )
    if policy.site_host.casefold() != profile.site_host.casefold():
        raise BrowserAccountSelectionRefused("account_policy_mismatch")

    return BindAuthorizationFact(
        BIND_AUTHORIZATION_KIND,
        fact.selection_id,
        fact.selection_revision,
        fact.binding_key,
        fact.task_scope,
        fact.grant_id,
        fact.grant_revision,
        fact.account_id,
        profile,
        policy,
        fact.source,
        fact.retention_source,
        fact.policy_source,
        tuple(fact.allowed_hosts),
    )


def authorize_bind_from_selection_record(
    store,
    *,
    selection_id: str,
    binding: BrowserBinding,
    keep: bool = False,
) -> BindAuthorizationFact:
    """Selection record → re-read grant → BindAuthorizationFact (no browser)."""
    fact = _load_fact(store, selection_id)
    return _authorize_from_fact(store, fact, binding=binding, keep=keep)


def selection_dispatch_guard(
    store,
    authorization: BindAuthorizationFact,
) -> Callable[[], bool]:
    """Return a zero-arg guard that rechecks authorization without opening a browser.

    True means the selection is still grant-backed and retention-confirmed.
    Never raises into a browser runtime; failures collapse to False.
    """
    if type(authorization) is not BindAuthorizationFact:
        raise BrowserAccountSelectionRefused("account_source_invalid")

    binding = BrowserBinding(
        project_id=authorization.task_scope.project_id,
        customer_id=authorization.task_scope.customer_id,
        environment_id=authorization.task_scope.environment_id,
        task_id=authorization.task_scope.task_id,
    )

    def _guard() -> bool:
        try:
            fresh = authorize_bind_from_selection_record(
                store,
                selection_id=authorization.selection_id,
                binding=binding,
                keep=authorization.profile_scope.keep,
            )
        except BrowserAccountSelectionRefused:
            return False
        except Exception:
            return False
        return (
            fresh.selection_id == authorization.selection_id
            and fresh.selection_revision == authorization.selection_revision
            and fresh.grant_id == authorization.grant_id
            and fresh.grant_revision == authorization.grant_revision
            and fresh.account_id == authorization.account_id
            and fresh.source == authorization.source
            and fresh.retention_source == authorization.retention_source
            and fresh.policy_source == authorization.policy_source
        )

    return _guard


def apply_bind_authorization(
    registry,
    binding: BrowserBinding,
    authorization: BindAuthorizationFact,
    *,
    verification_only: bool = False,
) -> None:
    """Attach ProfileScope via registry.bind_account_profile (in-memory only)."""
    if type(authorization) is not BindAuthorizationFact:
        raise BrowserAccountSelectionRefused("account_source_invalid")
    if type(binding) is not BrowserBinding:
        raise BrowserAccountSelectionRefused("account_source_invalid")
    if not _binding_matches(binding, authorization.task_scope):
        raise BrowserAccountSelectionRefused("account_grant_scope_mismatch")
    binder = getattr(registry, "bind_account_profile", None)
    if binder is None or not callable(binder):
        raise BrowserAccountSelectionRefused("account_source_unavailable")
    binder(
        binding,
        authorization.profile_scope,
        login_policy=authorization.login_policy,
        verification_only=verification_only,
    )


def compose_agent_dispatch_guards(*guards: Callable[[], bool]) -> Callable[[], bool]:
    """AND-compose zero-arg dispatch guards. Empty → always True. Exceptions → False."""

    def _combined() -> bool:
        if not guards:
            return True
        for guard in guards:
            try:
                if not bool(guard()):
                    return False
            except Exception:
                return False
        return True

    return _combined


@contextmanager
def install_selection_agent_dispatch_guard(
    store,
    authorization: BindAuthorizationFact,
) -> Iterator[Callable[[], bool]]:
    """Install selection_dispatch_guard onto ``_AGENT_DISPATCH_GUARD`` for this scope.

    Process-local only: no Playwright, no Gateway Admission, no OS input. Yields the
    zero-arg guard that was installed. Always resets the ContextVar on exit.
    """
    from ampuly.layer4_tools.browser import _AGENT_DISPATCH_GUARD

    guard = selection_dispatch_guard(store, authorization)
    token = _AGENT_DISPATCH_GUARD.set(guard)
    try:
        yield guard
    finally:
        _AGENT_DISPATCH_GUARD.reset(token)

