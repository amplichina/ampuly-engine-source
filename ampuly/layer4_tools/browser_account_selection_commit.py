"""Commit dispatch-ready handoff into an immutable selection record (B06-C1).

Revalidates the handoff, re-reads the grant for RetentionEvidence.confirmed,
then appends a selection provenance row. Never advances catalog source_revision,
never opens a browser, never binds a profile, never touches dispatch_links.
"""
from __future__ import annotations

from ampuly.database.browser_account_grant_store import reader_from_ready_store
from ampuly.database.browser_account_selection_records import (
    SelectionRecordFact,
    commit_selection_record,
)
from ampuly.layer4_tools.browser_account_dispatch_ready import (
    DispatchReadyAccountSelection,
    revalidate_dispatch_ready_selection,
)
from ampuly.layer4_tools.browser_account_selection import (
    BrowserAccountSelectionRefused,
    RetentionEvidence,
    public_selection_error,
)

public_selection_commit_error = public_selection_error


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


def commit_dispatch_ready_selection(
    store,
    handoff: DispatchReadyAccountSelection,
    *,
    open_write,
    actor_ref: str,
    request_id: str,
    binding_key: str,
    previous_selection_id: str | None = None,
) -> SelectionRecordFact:
    """Handoff → revalidate → retention gate → short write → SelectionRecordFact."""
    if type(handoff) is not DispatchReadyAccountSelection:
        raise BrowserAccountSelectionRefused("account_source_invalid")
    if open_write is None or not callable(open_write):
        raise BrowserAccountSelectionRefused("account_source_invalid")

    fresh = revalidate_dispatch_ready_selection(store, handoff)
    reader = _reader(store)
    try:
        snapshot = reader.read_grant(
            fresh.grant_id,
            task_scope=fresh.resolved.task_scope,
        )
    except BrowserAccountSelectionRefused:
        raise
    except Exception:
        raise BrowserAccountSelectionRefused("account_source_unavailable") from None

    consent = getattr(snapshot, "retention", None)
    grant = getattr(snapshot, "grant", None)
    if (
        type(consent) is not RetentionEvidence
        or consent.confirmed is not True
        or grant is None
    ):
        raise BrowserAccountSelectionRefused("account_retention_unconfirmed")

    # Stamps on the fresh handoff must still match the re-read snapshot.
    if (
        grant.grant_id != fresh.grant_id
        or grant.grant_revision != fresh.grant_revision
        or snapshot.source != fresh.source
        or consent.source != fresh.retention_source
    ):
        raise BrowserAccountSelectionRefused("account_source_changed")

    with open_write() as conn:
        return commit_selection_record(
            conn,
            actor_ref=actor_ref,
            request_id=request_id,
            binding_key=binding_key,
            task_scope=fresh.resolved.task_scope,
            grant_id=fresh.grant_id,
            grant_revision=fresh.grant_revision,
            account_id=grant.account_id,
            source=fresh.source,
            retention_source=fresh.retention_source,
            policy_source=fresh.policy_source,
            retention_receipt_ref=grant.retention_receipt_ref,
            login_policy_ref=grant.login_policy_ref,
            allowed_hosts=fresh.allowed_hosts,
            previous_selection_id=previous_selection_id,
        )
