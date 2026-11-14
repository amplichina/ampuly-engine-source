"""Q5 shop-protocol skeleton: checklist readiness without claiming a wire.

``inbound_entry`` already refuses pending shop adapters. This module makes the
checklist itself unit-testable: callers may record which prerequisites they
believe they hold, but even a fully ticked checklist stays
``protocol_not_implemented`` until a real adapter is registered. A door that
reports success without a protocol is worse than a locked one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from ampuly.channels.inbound_entry import PENDING_ADAPTERS, SHOP_PENDING_NAMES, adapter_status


def checklist_slug(item: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(item or "").lower()).strip("_")[:80]


@dataclass
class ShopProtocolReadiness:
    channel: str
    canonical_channel: str
    available: bool
    state: str
    checklist: list[str] = field(default_factory=list)
    satisfied: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    checklist_complete: bool = False
    note: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "canonical_channel": self.canonical_channel,
            "available": self.available,
            "state": self.state,
            "checklist": list(self.checklist),
            "satisfied": list(self.satisfied),
            "missing": list(self.missing),
            "checklist_complete": self.checklist_complete,
            "note": self.note,
        }


def assess_shop_protocol(
    channel: str,
    evidence: Mapping[str, bool] | None = None,
) -> ShopProtocolReadiness:
    """Assess checklist evidence. Never flips available=True for pending shops."""
    name = str(channel or "").strip().lower()
    status = adapter_status(name)
    canonical = str(status.get("canonical_channel") or name)
    checklist = list(PENDING_ADAPTERS.get(canonical) or status.get("still_needed") or ())
    evidence_map = dict(evidence or {})

    if name not in SHOP_PENDING_NAMES and canonical not in PENDING_ADAPTERS:
        return ShopProtocolReadiness(
            channel=name,
            canonical_channel=canonical,
            available=False,
            state="not_a_shop_pending",
            checklist=checklist,
            note="not a pending shop protocol channel",
        )

    satisfied: list[str] = []
    missing: list[str] = []
    for item in checklist:
        ok = bool(evidence_map.get(item)) or bool(evidence_map.get(checklist_slug(item)))
        (satisfied if ok else missing).append(item)

    complete = bool(checklist) and not missing
    state = (
        "checklist_complete_protocol_not_implemented"
        if complete
        else "checklist_incomplete"
    )
    return ShopProtocolReadiness(
        channel=name,
        canonical_channel=canonical,
        available=False,
        state=state,
        checklist=checklist,
        satisfied=satisfied,
        missing=missing,
        checklist_complete=complete,
        note=(
            "checklist complete; wire adapter still required — refuse inbound"
            if complete
            else "shop protocol prerequisites still missing"
        ),
    )


@dataclass
class ShopSandboxEnvelope:
    """Local non-live normalization of claimed shop sandbox fields."""

    channel: str
    canonical_channel: str
    message_id: str = ""
    session_id: str = ""
    buyer_id: str = ""
    text: str = ""
    state: str = "envelope_incomplete"
    wire_ready: bool = False
    missing_fields: list[str] = field(default_factory=list)
    note: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "canonical_channel": self.canonical_channel,
            "message_id": self.message_id,
            "session_id": self.session_id,
            "buyer_id": self.buyer_id,
            "text": self.text,
            "state": self.state,
            "wire_ready": self.wire_ready,
            "missing_fields": list(self.missing_fields),
            "note": self.note,
        }


_REQUIRED_SANDBOX_FIELDS = ("message_id", "session_id", "buyer_id")


def normalize_shop_sandbox_envelope(
    channel: str,
    fields: Mapping[str, Any] | None = None,
) -> ShopSandboxEnvelope:
    """First non-live protocol step: normalize sandbox ids; never wire-ready.

    Exercises message_id / session_id field prep from the pending checklist
    without claiming an Alibaba push protocol or accepting inbound.
    """
    name = str(channel or "").strip().lower()
    status = adapter_status(name)
    canonical = str(status.get("canonical_channel") or name)
    raw = dict(fields or {})

    if name not in SHOP_PENDING_NAMES and canonical not in PENDING_ADAPTERS:
        return ShopSandboxEnvelope(
            channel=name,
            canonical_channel=canonical,
            state="not_a_shop_pending",
            wire_ready=False,
            note="not a pending shop protocol channel",
        )

    message_id = str(raw.get("message_id") or "").strip()
    session_id = str(raw.get("session_id") or "").strip()
    buyer_id = str(raw.get("buyer_id") or "").strip()
    text = str(raw.get("text") or "")
    missing = [key for key in _REQUIRED_SANDBOX_FIELDS if not str(raw.get(key) or "").strip()]

    if missing:
        return ShopSandboxEnvelope(
            channel=name,
            canonical_channel=canonical,
            message_id=message_id,
            session_id=session_id,
            buyer_id=buyer_id,
            text=text,
            state="envelope_incomplete",
            wire_ready=False,
            missing_fields=missing,
            note="sandbox envelope missing required correlation fields",
        )

    return ShopSandboxEnvelope(
        channel=name,
        canonical_channel=canonical,
        message_id=message_id,
        session_id=session_id,
        buyer_id=buyer_id,
        text=text,
        state="envelope_normalized_not_wired",
        wire_ready=False,
        missing_fields=[],
        note="normalized locally; push protocol / outbound still not implemented",
    )

