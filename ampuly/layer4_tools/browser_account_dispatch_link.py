"""Open an ordered dispatch link from a selection record (B06-C3).

Re-authorizes bind from the selection record, then appends an immutable
dispatch link with the next local attempt_seq. Never opens a browser, never
admits Gateway actions, never wires true-input agent dispatch-guard, never advances
catalog source_revision.
"""
from __future__ import annotations

from ampuly.database.browser_account_dispatch_links import (
    DispatchLinkFact,
    commit_dispatch_link,
)
from ampuly.layer4_tools.browser_account_bind_guard import (
    BindAuthorizationFact,
    authorize_bind_from_selection_record,
)
from ampuly.layer4_tools.browser_account_selection import (
    BrowserAccountSelectionRefused,
    public_selection_error,
)
from ampuly.layer4_tools.browser_sessions import BrowserBinding

public_dispatch_link_error = public_selection_error


def open_dispatch_link_from_selection(
    store,
    *,
    selection_id: str,
    binding: BrowserBinding,
    open_write,
    actor_ref: str,
    request_id: str,
    keep: bool = False,
) -> DispatchLinkFact:
    """Authorize bind from selection → short write → DispatchLinkFact (ordered)."""
    if open_write is None or not callable(open_write):
        raise BrowserAccountSelectionRefused("account_source_invalid")
    if type(actor_ref) is not str or type(request_id) is not str:
        raise BrowserAccountSelectionRefused("account_source_invalid")

    authorization = authorize_bind_from_selection_record(
        store,
        selection_id=selection_id,
        binding=binding,
        keep=keep,
    )
    if type(authorization) is not BindAuthorizationFact:
        raise BrowserAccountSelectionRefused("account_source_invalid")

    with open_write() as conn:
        return commit_dispatch_link(
            conn,
            selection_id=authorization.selection_id,
            selection_revision=authorization.selection_revision,
            actor_ref=actor_ref,
            request_id=request_id,
            binding_key=authorization.binding_key,
            grant_id=authorization.grant_id,
            grant_revision=authorization.grant_revision,
            account_id=authorization.account_id,
            source=authorization.source,
            retention_source=authorization.retention_source,
            policy_source=authorization.policy_source,
        )
