"""Controlled listing submit/read-back contract (UI via Ego; not an IM wire).

A21 C01/C03 need a *bounded* platform submit + read-back. Alibaba shop IM
(``wire_ready`` / ``accept_inbound``) stays locked — that is a different door.
This module only describes the listing/publish UI contract and records whether
a draft/warehouse cycle actually completed. It never claims a push protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


LISTING_REQUIRED = (
    "seller_session_logged_in",
    "publish_router_reachable",
    "category_selected",
    "draft_or_warehouse_submit",
    "warehouse_or_draft_readback",
)


@dataclass
class ListingControlledReadiness:
    channel: str = "taobao_listing"
    available: bool = False
    state: str = "checklist_incomplete"
    checklist: list[str] = field(default_factory=lambda: list(LISTING_REQUIRED))
    satisfied: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    checklist_complete: bool = False
    wire_ready: bool = False  # IM wire — always False here
    note: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "available": self.available,
            "state": self.state,
            "checklist": list(self.checklist),
            "satisfied": list(self.satisfied),
            "missing": list(self.missing),
            "checklist_complete": self.checklist_complete,
            "wire_ready": False,
            "note": self.note,
            "evidence": dict(self.evidence),
        }


def assess_listing_controlled(
    evidence: Mapping[str, bool] | None = None,
    *,
    detail: Mapping[str, Any] | None = None,
) -> ListingControlledReadiness:
    """Tick listing UI prerequisites. Never flips IM wire_ready."""
    evidence_map = {str(k): bool(v) for k, v in dict(evidence or {}).items()}
    satisfied = [item for item in LISTING_REQUIRED if evidence_map.get(item)]
    missing = [item for item in LISTING_REQUIRED if item not in satisfied]
    complete = not missing
    # available only when the full draft submit+readback cycle is evidenced
    available = complete
    if complete:
        state = "listing_ui_contract_met"
        note = (
            "draft/warehouse submit+readback evidenced via Ego UI; "
            "IM wire_ready remains False; mass publish not claimed"
        )
    elif not evidence_map:
        state = "checklist_incomplete"
        note = "no listing UI evidence yet"
    else:
        state = "checklist_incomplete"
        note = "listing UI prerequisites still missing: " + ", ".join(missing)
    return ListingControlledReadiness(
        available=available,
        state=state,
        satisfied=satisfied,
        missing=missing,
        checklist_complete=complete,
        wire_ready=False,
        note=note,
        evidence=dict(detail or {}),
    )
