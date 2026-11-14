"""Admit a Gateway READ_ONLY action from a dispatch link (B06-C5).

Maps ``DispatchLinkFact`` to a Gateway ``attempt_id`` and submits through
``ComputerUseGateway``. No live Playwright, no live browser-bridge admit
path, no page input, no settle/execute. attempt_seq stays a local ordering
point; the Gateway attempt_id is a derived identifier, not the seq itself.
"""
from __future__ import annotations

from typing import Any

from ampuly.computer_use.contracts import ACTION_KINDS, ActionClass
from ampuly.computer_use.gateway import Admission, ComputerUseGateway
from ampuly.database.browser_account_dispatch_links import (
    DISPATCH_LINK_KIND,
    DispatchLinkFact,
)
from ampuly.layer4_tools.browser_account_selection import BrowserAccountSelectionRefused

DISPATCH_ADMIT_KIND = "dispatch_admit_v1"


def gateway_attempt_id_for_dispatch_link(link: DispatchLinkFact) -> str:
    """Deterministic Gateway attempt_id for one immutable dispatch link.

    Format ``{link_id}:s:{attempt_seq}`` — link_id is ``bdl_``+32 hex; seq >= 1.
    This is not equal to attempt_seq and does not by itself enable page input.
    """
    if type(link) is not DispatchLinkFact:
        raise BrowserAccountSelectionRefused("account_source_invalid")
    if link.record_kind != DISPATCH_LINK_KIND:
        raise BrowserAccountSelectionRefused("account_source_invalid")
    if type(link.link_id) is not str or not link.link_id.startswith("bdl_"):
        raise BrowserAccountSelectionRefused("account_source_invalid")
    if type(link.attempt_seq) is not int or link.attempt_seq < 1:
        raise BrowserAccountSelectionRefused("account_source_invalid")
    return f"{link.link_id}:s:{link.attempt_seq}"


def admit_read_only_from_dispatch_link(
    gateway: ComputerUseGateway,
    session_id: str,
    link: DispatchLinkFact,
    *,
    action_id: str,
    observation_hash: str,
    scope_digest: str,
    control_lease_id: str,
    kind: str = "find",
    target: dict[str, Any] | None = None,
    expected_effect: str = "locate via dispatch link",
    timeout_sec: float = 10.0,
) -> Admission:
    """Submit one READ_ONLY Gateway action whose attempt_id derives from ``link``.

    Refuses non-READ_ONLY kinds. Does not call the live browser bridge, does not
    run Playwright, and does not settle the admission.
    """
    if type(gateway) is not ComputerUseGateway:
        raise BrowserAccountSelectionRefused("account_source_invalid")
    if type(session_id) is not str or not session_id:
        raise BrowserAccountSelectionRefused("account_source_invalid")
    if type(kind) is not str or kind not in ACTION_KINDS:
        raise BrowserAccountSelectionRefused("account_source_invalid")
    if ACTION_KINDS[kind] is not ActionClass.READ_ONLY:
        raise BrowserAccountSelectionRefused("account_source_invalid")
    attempt_id = gateway_attempt_id_for_dispatch_link(link)
    payload = {
        "action_id": action_id,
        "attempt_id": attempt_id,
        "kind": kind,
        "scope_digest": scope_digest,
        "control_lease_id": control_lease_id,
        "observation_hash": observation_hash,
        "target": dict(target or {"ref": "dispatch-link"}),
        "expected_effect": expected_effect,
        "risk": ActionClass.READ_ONLY.value,
        "timeout_sec": timeout_sec,
        "idempotent": True,
        "retryable": True,
    }
    return gateway.submit(session_id, payload)
