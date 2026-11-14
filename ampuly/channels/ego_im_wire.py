"""Ego-browser UI wire for 旺旺/千牛客服 (engine-owned; not Alibaba push).

Official `alibaba_shop` push protocol stays pending (ISV资质). This module
describes a *separate* control path: Ampuly drives the seller CS page already
open in Ego Profile `sandbox-shop` — session list + composer visible, optional
dry-run fill. `wire_ready` here means Ego UI control is proven, never that
push callbacks or outbound APIs are live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

EGO_IM_REQUIRED = (
    "seller_session_logged_in",
    "cs_home_reachable",
    "im_chrome_visible",
    "composer_visible",
)

WIRE_KIND = "ego_browser_ui"


@dataclass
class EgoImWireReadiness:
    channel: str = "taobao_qn_ego_im"
    wire_kind: str = WIRE_KIND
    available: bool = False
    wire_ready: bool = False
    state: str = "checklist_incomplete"
    checklist: list[str] = field(default_factory=lambda: list(EGO_IM_REQUIRED))
    satisfied: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    checklist_complete: bool = False
    alibaba_push_protocol: bool = False
    note: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "wire_kind": self.wire_kind,
            "available": self.available,
            "wire_ready": self.wire_ready,
            "state": self.state,
            "checklist": list(self.checklist),
            "satisfied": list(self.satisfied),
            "missing": list(self.missing),
            "checklist_complete": self.checklist_complete,
            "alibaba_push_protocol": False,
            "note": self.note,
            "evidence": dict(self.evidence),
        }


def assess_ego_im_wire(
    evidence: Mapping[str, bool] | None = None,
    *,
    detail: Mapping[str, Any] | None = None,
    dry_run_fill_ok: bool = False,
) -> EgoImWireReadiness:
    """Tick Ego UI IM prerequisites. Never claims Alibaba push."""
    evidence_map = {str(k): bool(v) for k, v in dict(evidence or {}).items()}
    satisfied = [item for item in EGO_IM_REQUIRED if evidence_map.get(item)]
    missing = [item for item in EGO_IM_REQUIRED if item not in satisfied]
    complete = not missing
    # wire_ready only when chrome+composer proven; dry-run fill strengthens note
    wire_ready = complete
    if complete:
        state = "ego_im_wire_ready"
        note = (
            "Ego CS UI control evidenced (session chrome + composer); "
            "alibaba_shop push protocol still pending/ISV; inbound push not claimed"
        )
        if dry_run_fill_ok:
            note += "; composer dry-run fill ok (no send)"
    elif not evidence_map:
        state = "checklist_incomplete"
        note = "no Ego IM evidence yet"
    else:
        state = "checklist_incomplete"
        note = "Ego IM prerequisites still missing: " + ", ".join(missing)
    return EgoImWireReadiness(
        available=wire_ready,
        wire_ready=wire_ready,
        state=state,
        satisfied=satisfied,
        missing=missing,
        checklist_complete=complete,
        alibaba_push_protocol=False,
        note=note,
        evidence=dict(detail or {}),
    )
