"""The Feishu card of an order's delivery to its 企业微信 customer (W3b, policy「按推荐做」).

An order that came in through 企业微信客服 and is now in 客户验收中 gets one card per arrival there: the drafted message
to the customer, the files that go with it (the chooser when there are more than fit), and two buttons, 放行发给客户 /
先不发. Nothing goes to the customer without the owner's 放行 on this card (feishu-in-place-review: the order, where it
stands and the files are on the card; nothing leads out of Feishu). Its gate_cards row is keyed
``wecom-delivery:<order_id>:<n>``, n counting the order's arrivals in 客户验收中, like the close card.

A card kind of the console's Feishu gate (``extensions.FeishuCardKind``, ``CARD_KIND``): the gate service finds its
spec (rebuilt by ``wecom_delivery.GateHooks.load_spec`` after a restart) and hands a press to ``on_event``; the duty
round's retire sweep asks ``retire_reason``. Everything here uses only what the gate service offers every card kind.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from ampuly.plugins.office_ui.extensions import FeishuCardKind
from ampuly.plugins.office_ui.feishu_gate import (
    MAX_CARD_IMAGES,
    GateAction,
    GateSpec,
    card_for,
    decision_word,
    delivery_spell,
    order_line,
)

WECOM_DELIVERY_KIND = "wecom_delivery"
WECOM_DELIVERY_PREFIX = "wecom-delivery:"
WECOM_DELIVERY_LABEL = "发给企业微信客户"
WECOM_RELEASE = "wecom_release"
WECOM_HOLD = "wecom_hold"
WECOM_RELEASE_LABEL = "放行发给客户"
WECOM_HOLD_LABEL = "先不发"
WECOM_DELIVERY_DECISIONS = frozenset({WECOM_RELEASE, WECOM_HOLD})
# At most this many files go with one delivery: 企业微信 lets the enterprise send 5 messages after the customer's last
# one, and the first is the message itself.
WECOM_DELIVERY_MAX_FILES = 4
WECOM_DELIVERY_NOTE = (
    "放行时先核对企业微信的规矩（客户最后一次发消息后 48 小时内、最多 5 条、会话不在人工接待中）：能发就发 1 条说明加文件；"
    "发不了会在这里说明原因，交付包已经备好，可以在企业微信里手动转。发出去没有回音的，系统不会自己重发。只有老板能点。"
)
WECOM_DELIVERY_TYPED_REPLY = "这张卡只认「放行发给客户」和「先不发」两个按钮，在话题里回复不会处理。"
WECOM_DELIVERY_OWNER_ONLY = "只有老板能把交付发给客户。老板的飞书账号要先在设置里填好，否则飞书上谁都发不了。"
WECOM_DELIVERY_BUSY = "上一次点击还在处理，请稍等再看。"
WECOM_DELIVERY_FAILED = "处理时出错了，这次没有确认发出去什么；请到企业微信里看一眼客户收到了哪些。"
WECOM_DELIVERY_UNAVAILABLE = "现在没法在飞书上把交付发给客户，这次什么都没有发。"
WECOM_DELIVERY_MOVED_ON = {
    "order_closed": "这张工单已经结案了，这张卡不再发东西。",
    "order_cancelled": "这张工单已经取消了，这张卡不再发东西。",
    "superseded": "这张卡已作废，请看新卡。",
    "void": "这张工单已经不在客户验收中了，这张卡不再发东西。",
}
# A delivery is sent under a claim on its card for as long as the uploads and sends take; a press after this long may take
# the claim over, and is then told the earlier press's outcome is unknown (never sent again).
WECOM_DELIVERY_CLAIM_STALE_SECONDS = 1800.0


def wecom_delivery_key(order_id: str, spell: int = 1) -> str:
    """The gate_cards key of an order's delivery card for its ``spell``-th arrival in 客户验收中."""
    return f"{WECOM_DELIVERY_PREFIX}{order_id}:{max(1, int(spell or 1))}"


def wecom_delivery_target(checkpoint_id: str) -> tuple[str, int] | None:
    """(order_id, spell) of a delivery-card key; None for any other card."""
    text = str(checkpoint_id or "")
    if not text.startswith(WECOM_DELIVERY_PREFIX):
        return None
    order_id, _sep, spell = text[len(WECOM_DELIVERY_PREFIX):].rpartition(":")
    if not order_id.strip() or not spell.isdigit():
        return None
    return order_id, int(spell)


def _megabytes_text(size: Any) -> str:
    value = max(0, int(size or 0))
    if value < 1024 * 1024:
        return f"{max(1, round(value / 1024))} KB"
    return f"{value / (1024 * 1024):.1f} MB"


def build_wecom_delivery_card(spec: GateSpec, image_keys: list[str], *, status_line: str = "") -> dict[str, Any]:
    """The delivery card: the order, who the customer is, the message, the files, the owner's two buttons.

    With more files than one delivery takes the card carries a chooser (the drafted default ticked) inside a form whose
    submit is 放行发给客户. With ``status_line`` (answered, or retired) the buttons go and the line says why.
    """
    info = dict(spec.delivery or {})
    files = [dict(item) for item in list(info.get("files", []) or []) if isinstance(item, dict)]
    chosen = [str(item) for item in list(info.get("default", []) or [])]
    elements: list[dict[str, Any]] = []
    order = order_line(spec)
    if order:
        elements.append({"tag": "markdown", "content": order})
    if not status_line:
        elements.append({"tag": "markdown", "content": "**现在：客户验收中。**给客户的交付已经备好，你放行后才会经企业微信发给客户。"})
    who = str(info.get("customer", "") or "").strip()
    if who:
        elements.append({"tag": "markdown", "content": f"**客户**：{who}（从企业微信客服来的单）"})
    window = str(info.get("window", "") or "").strip()
    if window and not status_line:
        elements.append({"tag": "markdown", "content": f"**现在能不能发**：{window}"})
    if spec.prompt:
        elements.append({"tag": "markdown", "content": "**要发给客户的话**\n" + spec.prompt})
    if files:
        lines = [f"{index}. {item.get('name', '')}（{_megabytes_text(item.get('size'))}，{'图片' if item.get('media_type') == 'image' else '文件'}）"
                 + ("" if len(files) <= WECOM_DELIVERY_MAX_FILES or str(item.get("id", "")) in chosen else "（默认不发）")
                 for index, item in enumerate(files, 1)]
        head = f"**要发的文件**（{min(len(files), WECOM_DELIVERY_MAX_FILES)} 个）" if len(files) <= WECOM_DELIVERY_MAX_FILES else \
            f"**交付文件 {len(files)} 个，一次最多发 {WECOM_DELIVERY_MAX_FILES} 个**：默认勾了前 {len(chosen)} 个，放行前可以在下面改"
        elements.append({"tag": "markdown", "content": head + "\n" + "\n".join(lines)})
    else:
        elements.append({"tag": "markdown", "content": "**要发的文件**：没有能由系统发的文件，只发上面这句话。"})
    for key in image_keys[:MAX_CARD_IMAGES]:
        elements.append({"tag": "img", "img_key": key, "alt": {"tag": "plain_text", "content": "交付图片"}})
    too_large = [dict(item) for item in list(info.get("too_large", []) or []) if isinstance(item, dict)]
    if too_large:
        elements.append({"tag": "markdown", "content": "**这些文件超过企业微信的上限（图片 10MB、文件 20MB），系统发不了，请在企业微信里手动转**：\n"
                         + "\n".join(f"· {item.get('name', '')}（{_megabytes_text(item.get('size'))}）" for item in too_large)})
    more = int(info.get("more_than_listed", 0) or 0)
    if more:
        elements.append({"tag": "markdown", "content": f"另有 {more} 个交付文件没有列在这里，需要的话请在企业微信里手动转。"})
    package = str(info.get("package", "") or "").strip()
    if package:
        elements.append({"tag": "markdown", "content": f"**交付包**：{package}"})
    if status_line:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": status_line}]})
        header_color = "grey"
    else:
        release = {"gate": spec.checkpoint_id, "version": spec.version, "action": WECOM_RELEASE}
        hold = {"gate": spec.checkpoint_id, "version": spec.version, "action": WECOM_HOLD}
        hold_button = {"tag": "button", "text": {"tag": "plain_text", "content": WECOM_HOLD_LABEL}, "type": "default", "value": hold}
        if len(files) > WECOM_DELIVERY_MAX_FILES:
            # JSON 1.0 card: the chooser and the submit that carries it share a named form.
            elements.append({"tag": "form", "name": "wecom_delivery", "elements": [
                {"tag": "multi_select_static", "name": "files",
                 "placeholder": {"tag": "plain_text", "content": f"选要发的文件（最多 {WECOM_DELIVERY_MAX_FILES} 个）"},
                 "selected_values": chosen,
                 "options": [{"text": {"tag": "plain_text", "content": str(item.get("name", ""))[:60]}, "value": str(item.get("id", ""))}
                             for item in files]},
                {"tag": "button", "name": "release", "action_type": "form_submit", "type": "primary",
                 "text": {"tag": "plain_text", "content": WECOM_RELEASE_LABEL}, "value": release},
            ]})
            elements.append({"tag": "action", "actions": [hold_button]})
        else:
            elements.append({"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": WECOM_RELEASE_LABEL}, "type": "primary", "value": release},
                hold_button,
            ]})
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": WECOM_DELIVERY_NOTE}]})
        header_color = "blue"
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": header_color, "title": {"tag": "plain_text", "content": spec.title}},
        "elements": elements,
    }


def retire_reason(service: Any, checkpoint_id: str) -> tuple[str, str]:
    """(why a delivery card is retired now, the newer delivery card of the same order); ("", "") while it stands.

    The same rule as the close card's (``GateService.order_close_retire_reason``): the card stands while its order is in
    客户验收中 on the arrival it was drafted for; ("", "") too when the order cannot be read.
    """
    target = wecom_delivery_target(checkpoint_id)
    if target is None:
        return "", ""
    order_id, spell = target
    try:
        order = service.business.get_order(order_id)
        if order is None:
            return "void", ""
        ended = service.order_end(checkpoint_id)
        if ended in {"cancelled", "closed"}:
            return ("order_cancelled" if ended == "cancelled" else "order_closed"), ""
        if str(getattr(order, "status", "") or "") != "delivering":
            return "void", ""
        now = delivery_spell(service.business.events(order_id))
    except Exception:
        logger.opt(exception=True).debug("feishu gate: the order behind delivery card {} could not be read", checkpoint_id[:40])
        return "", ""
    if now == spell:
        return "", ""
    newer = wecom_delivery_key(order_id, now)
    return ("superseded", newer) if now > spell and service.business.gate_sent_or_decided(newer) else ("void", "")


async def on_event(service: Any, spec: GateSpec, action: GateAction, hooks: Any = None) -> dict[str, Any]:
    """What happens on a delivery card: its two buttons, the owner's only; a reply or an upload is told so and taken
    nowhere; a read receipt or a look records nothing.

    A press is claimed like a close (one at a time; a second press while the first sends is told to wait). The
    channel's answer (``hooks.answer``, ``wecom_delivery.GateHooks``) says whether it is final: 先不发, or anything that
    went (or may have gone) to the customer, makes the card read its outcome with no buttons; nothing sent at all (the
    48 hours are over, the 5 are used, a person has the session, no link to the relay) gives the card back with the
    reason and where the package is in its thread.
    """
    business = service.business
    target = str(action.message_id or (business.gate_card(spec.checkpoint_id) or {}).get("message_id", "") or "")
    if action.kind in {"read", "view"}:
        return {"ignored": "not_a_review"}
    if action.kind not in WECOM_DELIVERY_DECISIONS:
        await service.say(target, WECOM_DELIVERY_TYPED_REPLY)
        return {"ignored": "not_a_delivery_answer"}
    person = service.employee_by_open_id(action.actor_open_id) or {}
    if str(person.get("role", "") or "").strip().lower() != "owner":
        business.record_gate_event(spec.checkpoint_id, "unauthorized_attempt", actor=action.actor_open_id, note={"kind": action.kind})
        logger.warning("feishu gate: {} on the delivery of order {} by {} refused: not the owner", action.kind, spec.order_id, action.actor_open_id[:10])
        await service.say(target, WECOM_DELIVERY_OWNER_ONLY)
        return {"ignored": "unauthorized"}
    reason, newer = retire_reason(service, spec.checkpoint_id)
    if reason:
        outcome = await service.retire(spec.checkpoint_id, reason, spec=spec, replaced_by=newer)
        await service.say(target, WECOM_DELIVERY_MOVED_ON.get(reason, WECOM_DELIVERY_MOVED_ON["void"]))
        return {"ignored": "moved_on", "reason": reason, **({"retired": outcome["retired"]} if outcome.get("retired") else {})}
    answer = getattr(hooks, "answer", None)
    if not callable(answer):
        await service.say(target, WECOM_DELIVERY_UNAVAILABLE)
        return {"ignored": "no_delivery"}
    actor = str(person.get("name") or person.get("login") or "")
    decided_by = str(person.get("employee_id") or actor)
    token = business.claim_gate_decision(
        spec.checkpoint_id, decision=action.kind, decided_by=decided_by, note="", task_id=spec.task_id,
        order_id=spec.order_id, version=spec.version, stale_after_seconds=WECOM_DELIVERY_CLAIM_STALE_SECONDS,
    )
    if not token:
        busy = service.why_not_free(spec.checkpoint_id) or {"ignored": "decision_in_progress"}
        await service.say(target, "这张卡已经处理过了。" if busy.get("ignored") == "already_decided" else WECOM_DELIVERY_BUSY)
        return busy
    try:
        result = dict(await answer(spec, action.kind, dict(person), dict(action.form or {})) or {})
    except BaseException as exc:
        # The channel records where a send got to before it starts sending: a press that takes this card again is
        # told the outcome is unknown and sends nothing (``wecom_delivery``), so giving the claim back is safe.
        business.release_gate_decision(spec.checkpoint_id, token)
        if not isinstance(exc, Exception):
            raise
        logger.opt(exception=exc).warning("feishu gate: the delivery of order {} stopped on an error", spec.order_id)
        business.record_gate_event(spec.checkpoint_id, "wecom_delivery_failed", actor=decided_by, note={"cause": type(exc).__name__})
        await service.say(target, WECOM_DELIVERY_FAILED)
        return {"error": "delivery_failed"}
    said = str(result.get("say", "") or "").strip()
    if not result.get("final"):
        business.release_gate_decision(spec.checkpoint_id, token)
        if said:
            await service.say(target, said)
        return {"not_sent": str(result.get("reason", "") or "")}
    if not business.finish_gate_decision(spec.checkpoint_id, token):
        logger.warning("feishu gate: the delivery card of order {} was answered, but its claim was taken over meanwhile", spec.order_id)
    business.record_gate_event(spec.checkpoint_id, action.kind, actor=decided_by, note={
        "note": decision_word(action.kind), "outcome": str(result.get("outcome", "") or ""), "channel": "feishu"})
    status = str(result.get("status_line", "") or "").strip() or f"{decision_word(action.kind)} · {actor}"
    row = business.gate_card(spec.checkpoint_id) or {}
    if service.client is not None and row.get("message_id"):
        try:
            await service.client.update_card(str(row["message_id"]), card_for(spec, [], status_line=status))
        except Exception:
            logger.opt(exception=True).warning("feishu gate: the delivery card of order {} could not be updated", spec.order_id)
    if said:
        await service.say(target, said)
    return {"decided": action.kind, "by": actor, "outcome": str(result.get("outcome", "") or "")}


def _hooks(console: Any) -> Any:
    from ampuly.plugins.wecom_service.wecom_delivery import GateHooks

    return GateHooks(console)


CARD_KIND = FeishuCardKind(
    name=WECOM_DELIVERY_KIND,
    label=WECOM_DELIVERY_LABEL,
    decisions={WECOM_RELEASE: WECOM_RELEASE_LABEL, WECOM_HOLD: WECOM_HOLD_LABEL},
    target=wecom_delivery_target,
    build_card=build_wecom_delivery_card,
    on_event=on_event,
    retire_reason=retire_reason,
    hooks=_hooks,
    not_this_card="not_a_delivery_card",
)
