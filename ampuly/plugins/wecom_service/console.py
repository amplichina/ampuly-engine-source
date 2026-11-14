"""The 企业微信客服 extension as the console registers it: every seam the console offers, filled in one place.

What the console gets from this package, and nothing else (``extensions.ConsoleExtension``):

* the three owner-only ws actions (``wecom_channel.WS_HANDLERS``: status, the switch, a 客服账号's binding);
* the duty round: the relay link started or stopped where the round starts (``wecom_channel.ensure_for``), and the
  delivery cards of 企业微信 orders in 客户验收中 after the close cards (``wecom_delivery.announce_delivery_cards``);
* the stop: the relay link closed (``wecom_channel.stop_for``);
* the order record: an order from 企业微信客服 carries its conversation, read only, for the owner
  (``wecom_conversation``, ``wecom_delivery.conversation_view``);
* the Feishu card kind of a delivery (``delivery_card.CARD_KIND``);
* the project records of its three project tables (``records.PROJECT_RECORDS``);
* the 客户确认 lane's facts (``lane.read``);
* the contact channel ``wecom_kf``, the relay's two key-storage variables, the order source ``wecom_kf``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from loguru import logger

from ampuly.plugins.office_ui.extensions import ConsoleExtension
from ampuly.plugins.wecom_service import NAME
from ampuly.plugins.wecom_service.host import as_console_host


def _duty_round_start(console: Any) -> None:
    """The relay link runs only while the owner's switch is on, the relay is configured, outbound is not muted and the
    business database is at version 4; each round starts or stops it."""
    from ampuly.plugins.wecom_service.wecom_channel import ensure_for

    ensure_for(console)


async def _duty_round(console: Any) -> None:
    """An order from 企业微信客服 in 客户验收中 gets its delivery drafted and one 放行发给客户 card; nothing goes to the
    customer until the owner presses it. Off unless the channel is switched on."""
    from ampuly.plugins.wecom_service.wecom_delivery import announce_delivery_cards

    await announce_delivery_cards(console)


def _shutdown(console: Any) -> None:
    from ampuly.plugins.wecom_service.wecom_channel import stop_for

    stop_for(console)


async def _order_timeline_extra(console: Any, ws: Any, order: Mapping[str, Any]) -> dict[str, Any]:
    """An order from 企业微信客服 carries its conversation with the customer, read only, for the owner (W3b)."""
    from ampuly.plugins.wecom_service.wecom_channel import ORDER_SOURCE
    from ampuly.plugins.wecom_service.wecom_delivery import conversation_view

    host = as_console_host(console)
    if str(order.get("source", "") or "") != ORDER_SOURCE or not host.is_owner(ws):
        return {}
    order_id = str(order.get("order_id", "") or "").strip()
    try:
        conversation = await asyncio.to_thread(conversation_view, host.business, order_id)
    except Exception:  # noqa: BLE001 - the order record goes out without it
        logger.opt(exception=True).warning("order timeline: the 企业微信 conversation of order {} could not be read", order_id)
        return {}
    return {"wecom_conversation": conversation} if conversation is not None else {}


def extension() -> ConsoleExtension:
    from ampuly.plugins.wecom_service.delivery_card import CARD_KIND
    from ampuly.plugins.wecom_service.lane import read as read_lane
    from ampuly.plugins.wecom_service.records import PROJECT_RECORDS
    from ampuly.plugins.wecom_service.wecom_channel import CONTACT_CHANNEL, ORDER_SOURCE, SOURCE_WORD, WS_HANDLERS
    from ampuly.plugins.wecom_service.wecom_relay_client import RELAY_TOKEN_ENV, RELAY_URL_ENV

    return ConsoleExtension(
        name=NAME,
        ws_actions=dict(WS_HANDLERS),
        on_duty_round_start=_duty_round_start,
        on_duty_round=_duty_round,
        on_shutdown=_shutdown,
        order_timeline_extra=_order_timeline_extra,
        feishu_card=CARD_KIND,
        project_records=PROJECT_RECORDS,
        customer_lane=read_lane,
        contact_channels={CONTACT_CHANNEL: SOURCE_WORD},
        secret_env_names=(RELAY_URL_ENV, RELAY_TOKEN_ENV),
        order_source_words={ORDER_SOURCE: SOURCE_WORD},
    )
