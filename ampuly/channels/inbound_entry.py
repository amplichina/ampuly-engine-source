"""One door for messages arriving from anywhere, and an honest refusal.

Every channel normalises its own payload and then does the same four things:
decide whether this is a duplicate, decide whether the sender is allowed,
decide who the sender is, and decide which order the message is about. Doing
that four times means four places for it to drift.

The second reason this exists is the Alibaba shop channel, and it is the more
important one. We do not have that protocol. The tempting move is a stub that
accepts a message and returns success, so the button exists and the demo
works -- and then the first real customer message vanishes into it. So an
adapter with no protocol is registered as *pending*, with the list of what a
person has to obtain before it can be built, and calling it refuses. A door
that reports success without delivering is worse than a locked one, because
nobody goes looking for the messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# What somebody has to bring back before an adapter can be written. Named
# rather than described, because "the API docs" is not a thing anybody can
# tick off.
PENDING_ADAPTERS: dict[str, tuple[str, ...]] = {
    "alibaba_shop": (
        "官方消息推送协议文档（含回调签名与重试语义）",
        "一个可用的沙箱店铺与测试买家账号",
        "消息 id 与会话 id 的字段定义（用来去重和串起同一个买家）",
        "出站回复的接口与限流规则",
        "服务商资质：2026-09-09 现场核验，旺旺/千牛消息接口只对入驻服务商开放，主体须为有限责任公司或股份公司、注册资本 ≥ 100 万元、成立 ≥ 2 年；个人入驻拿不到消息接口",
    ),
}

# These names share only a pending checklist, NOT a proven wire protocol.
SHOP_PENDING_NAMES = frozenset({"taobao", "taobao_shop", "tmall", "tmall_shop", "1688", "1688_shop"})


class InboundNotSupported(RuntimeError):
    """This channel has no protocol yet, and pretending is worse than saying so."""

    def __init__(self, channel: str, missing: tuple[str, ...]) -> None:
        self.channel = channel
        self.missing = missing
        super().__init__(
            f"{channel} has no adapter yet; a door that reports success without delivering "
            f"is worse than a locked one. Still needed: {'; '.join(missing)}"
        )


@dataclass
class InboundDecision:
    """What happened to one arriving message, and why."""

    accepted: bool
    reason: str = ""
    duplicate: bool = False
    sender_allowed: bool = True
    customer_id: str = ""
    order_id: str = ""
    correlation_strength: str = "none"
    screen: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted, "reason": self.reason, "duplicate": self.duplicate,
            "sender_allowed": self.sender_allowed, "customer_id": self.customer_id,
            "order_id": self.order_id, "correlation_strength": self.correlation_strength,
            "screen": dict(self.screen),
        }


def adapter_status(channel: str) -> dict[str, Any]:
    """Catalog presence only; neither credentials nor a connected runtime."""
    from ampuly.channels.provider_registry import PROVIDER_SPECS

    name = str(channel or "").strip().lower()
    canonical = "alibaba_shop" if name in SHOP_PENDING_NAMES else name
    missing = PENDING_ADAPTERS.get(canonical)
    registered = name in PROVIDER_SPECS or name == "local_inbox"
    state = "pending_protocol" if missing else "adapter_present" if registered else "unregistered"
    if missing is None and not registered:
        missing = ("已实现并注册的渠道适配器与明确的协议契约",)
    return {
        "channel": name,
        "canonical_channel": canonical,
        "available": registered and missing is None,
        "state": state,
        "still_needed": list(missing or ()),
    }


def accept_inbound(
    *,
    channel: str,
    message_id: str,
    sender_id: str,
    content: str = "",
    reply_to: str = "",
    thread_id: str = "",
    dedup: Any = None,
    business: Any = None,
    is_allowed: Any = None,
) -> InboundDecision:
    """The four decisions, in the order that stops work soonest.

    Duplicate first: re-running the allow-list and the correlation for a
    message we already handled is wasted, and worse, a second correlation can
    disagree with the first.
    """
    from ampuly.channels.inbound_screen import screen_inbound

    name = str(channel or "").strip().lower()
    status = adapter_status(name)
    if not status["available"]:
        raise InboundNotSupported(name, tuple(status["still_needed"]))

    if dedup is not None and not dedup.remember(message_id):
        return InboundDecision(accepted=False, reason="already handled", duplicate=True)

    if callable(is_allowed) and not is_allowed(sender_id):
        # Not an error and not silence: somebody wrote to us and was turned
        # away, which an operator may want to know about.
        return InboundDecision(accepted=False, reason="sender is not on the allow list", sender_allowed=False)

    decision = InboundDecision(accepted=True)
    # Screening records what the content is trying to do. It never changes the
    # content and never grants anything.
    decision.screen = {"flags": list(screen_inbound(content) or [])}

    if business is not None:
        from ampuly.plugins.office_ui.contact_identity import correlate_message

        correlation = correlate_message(
            business, channel=name, identity=sender_id, reply_to=reply_to, thread_id=thread_id,
        )
        decision.customer_id = str(correlation.get("contact", {}).get("customer_id", "") or "")
        decision.order_id = str(correlation.get("order_id", "") or "")
        decision.correlation_strength = str(correlation.get("strength", "none") or "none")
        if not decision.order_id:
            # Accepted and unattached is a real state: somebody wrote in and
            # nobody can yet say what about. It reaches a person, not a bin.
            decision.reason = str(correlation.get("reason", "") or "not attached to an order")
    return decision
