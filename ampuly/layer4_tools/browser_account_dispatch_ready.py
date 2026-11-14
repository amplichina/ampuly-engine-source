"""Dispatch-ready account selection handoff from a ready project Store.

Read-only composition of B06-B2a ``reader_from_ready_store`` / Store reader entry
and B06-A ``read_account_selection``. No catalog writes, no selection_records,
no dispatch_links, no browser bind, no Gateway input.

A successful handoff is evidence that a trusted reader resolved one grant for an
authorized task scope *at that moment*. A later dispatch consumer must call
``revalidate_dispatch_ready_selection`` (and still apply B06-C1/C2 ordering)
before binding or enabling execution. This module never grants execution.
"""
from __future__ import annotations

from dataclasses import dataclass

from ampuly.database.browser_account_grant_store import reader_from_ready_store
from ampuly.layer4_tools.browser_account_selection import (
    AuthorizedTaskScope,
    BrowserAccountSelectionRefused,
    GrantSourceStamp,
    ResolvedAccountSelection,
    public_selection_error,
    read_account_selection,
)

HANDOFF_KIND = "dispatch_ready_selection_v1"


@dataclass(frozen=True, repr=False)
class DispatchReadyAccountSelection:
    """Immutable handoff for a later ordered dispatch consumer.

    Not an execution token: no lease, fence, attempt_id, or can_execute bit.
    Persistence of this fact belongs to B06-C1 (out of this module).
    """

    handoff_kind: str
    resolved: ResolvedAccountSelection
    grant_id: str
    grant_revision: int
    source: GrantSourceStamp
    retention_source: GrantSourceStamp
    policy_source: GrantSourceStamp
    allowed_hosts: tuple[str, ...]


def public_dispatch_ready_error(error: BaseException) -> dict[str, str]:
    """Same cross-tenant concealment as selection; do not invent a second dialect."""
    return public_selection_error(error)


def _as_handoff(resolved: ResolvedAccountSelection) -> DispatchReadyAccountSelection:
    if type(resolved) is not ResolvedAccountSelection:
        raise BrowserAccountSelectionRefused("account_source_invalid")
    return DispatchReadyAccountSelection(
        HANDOFF_KIND,
        resolved,
        resolved.grant_id,
        resolved.grant_revision,
        resolved.source,
        resolved.retention_source,
        resolved.policy_source,
        resolved.allowed_hosts,
    )


def prepare_dispatch_ready_selection(
    store,
    *,
    grant_id: str,
    task_scope: AuthorizedTaskScope,
    expected_source: GrantSourceStamp | None = None,
) -> DispatchReadyAccountSelection:
    """Ready Store → reader → one selection read+freshness check → handoff.

    Uses the Store entry point when present so opening-evidence contracts stay
    identical to B2a; falls back to ``reader_from_ready_store`` for plain stores.
    Never initializes, migrates, installs schema, or opens a browser.
    """
    reader = None
    try:
        entry = getattr(store, "browser_account_grant_reader", None)
        if callable(entry):
            reader = entry()
        else:
            reader = reader_from_ready_store(store)
    except BrowserAccountSelectionRefused:
        raise
    except Exception:
        raise BrowserAccountSelectionRefused("account_source_unavailable") from None
    resolved = read_account_selection(
        reader,
        grant_id=grant_id,
        task_scope=task_scope,
        expected_source=expected_source,
    )
    return _as_handoff(resolved)


def revalidate_dispatch_ready_selection(
    store,
    handoff: DispatchReadyAccountSelection,
    *,
    task_scope: AuthorizedTaskScope | None = None,
) -> DispatchReadyAccountSelection:
    """Fresh prepare; refuse if the prior handoff no longer matches catalog stamps.

    Still not a written dispatch fence. Callers that await between prepare and
    bind must revalidate again after the await.
    """
    if (
        type(handoff) is not DispatchReadyAccountSelection
        or handoff.handoff_kind != HANDOFF_KIND
        or type(handoff.resolved) is not ResolvedAccountSelection
    ):
        raise BrowserAccountSelectionRefused("account_source_invalid")
    scope = handoff.resolved.task_scope if task_scope is None else task_scope
    fresh = prepare_dispatch_ready_selection(
        store,
        grant_id=handoff.grant_id,
        task_scope=scope,
    )
    prior = (
        handoff.grant_id,
        handoff.grant_revision,
        handoff.source,
        handoff.retention_source,
        handoff.policy_source,
        handoff.allowed_hosts,
    )
    current = (
        fresh.grant_id,
        fresh.grant_revision,
        fresh.source,
        fresh.retention_source,
        fresh.policy_source,
        fresh.allowed_hosts,
    )
    if prior != current:
        raise BrowserAccountSelectionRefused("account_source_changed")
    return fresh
