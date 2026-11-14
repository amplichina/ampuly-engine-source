"""One person, several inboxes, one customer.

The same human is `ou_9f2c` in Feishu, a staff id in DingTalk and a phone
number in WhatsApp. Nothing joins those today, so a reply arriving on one
channel cannot be attached to the order that was discussed on another, and
"has the customer answered yet" is answered by a person remembering.

A contact is that join: a customer, a person, and the provider identities they
are known by. Two rules keep it from becoming a guess:

**An identity belongs to exactly one contact.** The same Feishu open_id cannot
be claimed by two customers -- that is not a merge, it is two people about to
read each other's delivery. It is refused, naming who holds it.

**An unrecognised identity stays unrecognised.** A message from an id nobody
has bound resolves to nothing, and the caller is told that. Guessing by name
or by "the only customer with an open order" is how one customer's file ends
up in another's folder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# A channel we can actually route on. Named rather than free text so a typo
# does not create a channel nobody delivers to. These are the console's own; a
# registered console extension adds the channels it binds identities on
# (``extensions.contact_channels``), and ``CHANNELS`` (``channels``) is both.
CORE_CHANNELS = ("feishu", "dingtalk", "wecom", "whatsapp", "telegram", "slack", "email", "local_inbox")
IDENTITY = re.compile(r"^[A-Za-z0-9_.@+\-]{2,128}$")


def channels() -> tuple[str, ...]:
    """Every channel an identity may be bound on: the console's own, then the registered extensions'."""
    from ampuly.plugins.office_ui import extensions

    return CORE_CHANNELS + tuple(name for name in extensions.contact_channels() if name not in CORE_CHANNELS)


def __getattr__(name: str) -> Any:
    # ``CHANNELS`` is read when asked (PEP 562), so it names the channels of the extensions registered then.
    if name == "CHANNELS":
        return channels()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class ContactError(ValueError):
    """A binding that would let two customers share an inbox is refused."""


@dataclass(frozen=True)
class ContactResolution:
    """Who this identity is, or the fact that nobody knows."""

    channel: str
    identity: str
    customer_id: str = ""
    contact_id: str = ""
    display_name: str = ""
    known: bool = False
    reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "channel": self.channel, "identity": self.identity,
            "customer_id": self.customer_id, "contact_id": self.contact_id,
            "display_name": self.display_name, "known": self.known, "reason": self.reason,
        }


def validate_channel(channel: str) -> str:
    clean = str(channel or "").strip().lower()
    if clean not in channels():
        raise ContactError(f"{channel!r} is not a channel this system delivers on")
    return clean


def validate_identity(identity: str) -> str:
    clean = str(identity or "").strip()
    if not IDENTITY.match(clean):
        raise ContactError(f"{identity!r} does not look like a provider identity")
    return clean


def resolve_identity(business: Any, *, channel: str, identity: str) -> ContactResolution:
    """Who wrote this, or an honest nobody.

    Never guesses. An unbound id resolves to ``known=False`` with the reason,
    because the alternative -- picking the only customer with an open order,
    or matching on a display name -- is how one customer's file ends up in
    another's folder.
    """
    try:
        clean_channel = validate_channel(channel)
        clean_identity = validate_identity(identity)
    except ContactError as exc:
        return ContactResolution(
            channel=str(channel or ""), identity=str(identity or ""), reason=str(exc),
        )
    try:
        row = business.contact_for_identity(channel=clean_channel, identity=clean_identity)
    except Exception:
        # Failing to read the mapping is not permission to guess at it.
        return ContactResolution(
            channel=clean_channel, identity=clean_identity, reason="contact_book_unreadable",
        )
    if not row:
        return ContactResolution(
            channel=clean_channel, identity=clean_identity, reason="unknown_identity",
        )
    return ContactResolution(
        channel=clean_channel,
        identity=clean_identity,
        customer_id=str(row.get("customer_id", "") or ""),
        contact_id=str(row.get("contact_id", "") or ""),
        display_name=str(row.get("display_name", "") or ""),
        known=True,
    )


def correlate_message(
    business: Any,
    *,
    channel: str,
    identity: str,
    reply_to: str = "",
    thread_id: str = "",
) -> dict[str, Any]:
    """Which order this message belongs to, and how sure that is.

    Three strengths, kept apart because they justify different actions:

    ``replied``   the message quotes one of ours, so it is about that order;
    ``threaded``  it arrived in a thread we opened for one order;
    ``sender``    we only know who wrote it, and that person has exactly one
                  open order -- enough to show them, not enough to act on.

    A sender with two open orders resolves to none. Picking one would be a
    coin flip presented as an answer.
    """
    who = resolve_identity(business, channel=channel, identity=identity)
    base: dict[str, Any] = {
        "contact": who.to_payload(), "order_id": "", "strength": "none", "reason": "",
    }
    if not who.known:
        base["reason"] = who.reason or "unknown_identity"
        return base
    for key, strength in ((str(reply_to or ""), "replied"), (str(thread_id or ""), "threaded")):
        if not key:
            continue
        try:
            order_id = str(business.order_for_outbound(key) or "")
        except Exception:
            order_id = ""
        if order_id:
            return {**base, "order_id": order_id, "strength": strength}
    try:
        open_orders = list(business.open_orders_for_customer(who.customer_id) or [])
    except Exception:
        open_orders = []
    if len(open_orders) == 1:
        return {**base, "order_id": str(open_orders[0]), "strength": "sender"}
    base["reason"] = (
        "no_open_order" if not open_orders
        else f"{len(open_orders)} open orders for this customer; which one is not established"
    )
    return base
