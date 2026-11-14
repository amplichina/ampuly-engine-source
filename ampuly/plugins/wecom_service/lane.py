"""企业微信客服's part of the project centre's 客户确认 lane (W3b, policy).

The project's bound 客服账号 and the owner's channel switch (default off) set the lane up; its real attempts are what
customers wrote there, each delivery message sent to them (sent, refused by 企业微信, or with no answer -- 结果未知) and
each customer 确认 recorded from 企业微信 (``WecomKfStore.lane_facts``, the acceptance rounds' ``decided_by``).

The console's seam (``extensions.ConsoleExtension.customer_lane``): ``read`` gives a ``WecomLane`` for one project,
plain data already read; ``project_connections`` applies it to the lane (``CustomerLaneExtra``). Nothing here sends.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# A delivery message's outcome as an attempt: sent True, refused by 企业微信 False, no answer (or never settled) None. A
# message the window never let go ("refused") was not tried and is no attempt.
_WECOM_DELIVERY_OUTCOMES: dict[str, bool | None] = {"sent": True, "failed": False, "outcome_unknown": None, "pending": None}


def _when(value: str) -> str:
    text = str(value or "").strip()
    return text[:16].replace("T", " ") if text else ""


@dataclass
class WecomLane:
    """The 客服账号 bound to the project (all, and switched on), whether the owner switched the channel on, when
    customers wrote (oldest first), each delivery message we tried to send {"at", "outcome"} (sent / failed /
    outcome_unknown / pending / refused)."""

    accounts: int = 0
    enabled_accounts: int = 0
    switch_on: bool = False
    customer_messages: list[str] = field(default_factory=list)
    deliveries: list[dict[str, str]] = field(default_factory=list)
    unbound_note: str = "企业微信客户各自登记成新客户"
    idle_note: str = "还没有和企业微信客户真正往来过"

    @property
    def present(self) -> bool:
        return self.accounts > 0

    def apply(self, facts: Any, sources: Any) -> list[Any]:
        """Configured by a bound 客服账号; the owner's switch (default off) and the outbound mute authorize it; its real
        attempts are the customers' messages that came in, each delivery message we tried to send (sent / refused by
        企业微信 / outcome unknown) and each 确认 recorded from 企业微信 (a round decided by ``CUSTOMER_ACTOR``)."""
        from ampuly.plugins.office_ui.project_connections import Attempt
        from ampuly.plugins.wecom_service.wecom_channel import CUSTOMER_ACTOR

        if self.accounts <= 0:
            return []
        facts.configured = True
        facts.evidence.append(f"企业微信客服账号 {self.accounts} 个接在这个项目上")
        if not self.switch_on:
            facts.authorized = False
            facts.evidence.append("企业微信通道还关着（默认关），在「配置 · 服务与连接 · 渠道连接」里打开")
        elif sources.channels_muted:
            facts.authorized = False
            facts.evidence.append("外发渠道已静音，打开后才会和企业微信客户往来")
        elif self.enabled_accounts <= 0:
            facts.authorized = False
            facts.evidence.append("接在这个项目上的客服账号都还没打开")
        attempts: list[Any] = []
        received = [str(item) for item in self.customer_messages if str(item)]
        if received:
            attempts.append(Attempt(True, received[-1],
                                    f"收到过企业微信客户的消息 {len(received)} 条，最近一次 {_when(received[-1])}"))
        tried = [row for row in self.deliveries if str(row.get("outcome", "") or "") in _WECOM_DELIVERY_OUTCOMES]
        for row in tried:
            ok = _WECOM_DELIVERY_OUTCOMES[str(row["outcome"])]
            word = "送达企业微信" if ok else ("企业微信没让发" if ok is False else "结果未知")
            attempts.append(Attempt(ok, str(row.get("at", "") or ""),
                                    f"经企业微信给客户发交付 {len(tried)} 条，最近一条{word}（{_when(row.get('at', ''))}）"))
        confirmed = sorted(str(item.get("at", "") or "") for item in list(sources.acceptance_decisions or [])
                           if str(item.get("by", "") or "").startswith(CUSTOMER_ACTOR))
        confirmed = [item for item in confirmed if item]
        if confirmed:
            attempts.append(Attempt(True, confirmed[-1],
                                    f"客户在企业微信里确认过交付 {len(confirmed)} 次，最近一次 {_when(confirmed[-1])}"))
        return attempts


def read(project_id: str, business: Any) -> WecomLane:
    """The project's accounts, the owner's switch, what customers wrote and what we tried to deliver. Read only; a store
    that cannot be read raises (the console then leaves this part out, as if the channel were not there)."""
    from ampuly.plugins.wecom_service.wecom_channel import switch_on
    from ampuly.plugins.wecom_service.wecom_kf_store import WecomKfStore

    facts = WecomKfStore(business).lane_facts(project_id)
    lane = WecomLane(
        accounts=int(facts["accounts"]), enabled_accounts=int(facts["enabled_accounts"]),
        customer_messages=list(facts["customer_messages"]), deliveries=list(facts["deliveries"]),
    )
    lane.switch_on = switch_on(business) if lane.accounts else False
    return lane
