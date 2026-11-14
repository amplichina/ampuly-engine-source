"""项目中心 · 软件与连接（S3-2A）：一个项目的四条线路各在什么状态，只从真实事实算。

The project page shows four lanes -- 业务来源 (intake), 内部协作 (review), 客户确认 (customer), 交付存储 (files) --
each with what the owner planned (which software, what is handed over, how, on which stage; the
``project_connection_plans`` table) and a status word. The words, and the only way each is reached:

* 未配置 ``unconfigured``: nothing real is set up for the lane. A plan is words about intent and never changes this.
* 待授权 ``needs_authorization``: set up, but a switch is off (a connector switch, the outbound mute) or a credential
  the lane needs is not there.
* 待验证 ``unverified``: set up and allowed, and no real attempt is on record.
* 可用 ``usable``: the last real attempt on record succeeded. **Never from configuration alone.**
* 失效 ``broken``: the last real attempt on record failed.
* 结果未知 ``unknown``: the last real attempt on record has no known outcome.

Where the facts come from (checked 2026-09-27; nothing here sends, connects or writes):

* the owner's plans -- ``BusinessStore.project_connection_plans`` (shown; status-neutral);
* which pager channels are wired and whether their values look right -- ``notifier.notify_status`` and
  ``notifier.channel_config_check`` (environment shape only, no send);
* the outbound mute and the connector switches somebody set -- ``notifier.channels_muted``,
  ``BusinessStore.connector_switch`` (default on);
* API connectors registered for the project -- ``connector_candidate.list_registrations``;
* a per-customer channel credential reference -- ``BusinessStore.credential_reference(purpose="channel")``
  (optional by design: a customer without one sends under the house account);
* **real attempts**: a Feishu gate card that reached the group (``gate_cards`` rows with a ``message_id``, written by
  feishu_gate after the send); this process's pager sends (the handler's in-memory duty log, ``sent[]`` per channel,
  ok or not -- gone after a restart); an order that came in (``work_orders``, by ``source``); a customer's acceptance
  decision recorded on an order (``acceptance`` rounds); a message we sent a customer about an order
  (``outbound_message_links`` -- the table exists, nothing writes it today); the project's folder probed right now
  (exists, writable);
* a registered console extension's part of the 客户确认 lane (``extensions``: its ``customer_lane`` reader gives a
  ``CustomerLaneExtra``): what it has set up on the project, what authorizes it, and its own real attempts.

What does not exist today, so the projection cannot claim it: a persisted result of a 「测试发送」 (notify_test
answers the console and records nothing); a record of a Feishu card send that failed (only successes are written);
a channel attempt with an unknown outcome (the kernel's effect journal keeps ``effect_outcome_unknown`` per order
effect and is not read here) -- so 结果未知 comes only from an extension's attempt whose answer never came.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ampuly.plugins.office_ui.business_store import (
    CONNECTION_LANE_LABELS,
    CONNECTION_LANES,
    CONNECTION_PLAN_FIELDS,
)

STATUS_WORDS = {
    "unconfigured": "未配置",
    "needs_authorization": "待授权",
    "unverified": "待验证",
    "usable": "可用",
    "broken": "失效",
    "unknown": "结果未知",
}

# The pager channels the console knows, by the words an owner writes for them.
_CHANNEL_WORDS = {
    "feishu": ("飞书", "feishu", "lark"),
    "dingtalk": ("钉钉", "dingtalk"),
}
# The console's own labels; a registered console extension adds its contact channels' (``extensions``).
_CORE_CHANNEL_LABELS = {"feishu": "飞书", "dingtalk": "钉钉"}
# "master": a work order 安安 drafted on 总控 from the owner's words (policy, M1; master_order_draft). A
# registered console extension adds the sources it creates orders from (``extensions.order_source_words``).
_CORE_SOURCE_WORDS = {"console": "控制台开单", "intake_flow": "需求表", "master": "安安起草"}
# How many of the project's newest orders the customer lane reads acceptance rounds for.
_ACCEPTANCE_SAMPLE = 20


def channel_labels() -> dict[str, str]:
    """Channel id -> the owner's word: the console's own, then the registered extensions' contact channels (never over
    one of the console's)."""
    from ampuly.plugins.office_ui import extensions

    labels = dict(_CORE_CHANNEL_LABELS)
    for name, word in extensions.contact_channels().items():
        labels.setdefault(name, word)
    return labels


def source_words() -> dict[str, str]:
    """Order ``source`` -> the owner's word: the console's own, then the registered extensions' (never over one of the
    console's)."""
    from ampuly.plugins.office_ui import extensions

    words = dict(_CORE_SOURCE_WORDS)
    for source, word in extensions.order_source_words().items():
        words.setdefault(source, word)
    return words


def __getattr__(name: str) -> Any:
    # ``_SOURCE_WORDS`` / ``_CHANNEL_LABELS`` are read when asked (PEP 562): the console's and the registered extensions'.
    if name == "_SOURCE_WORDS":
        return source_words()
    if name == "_CHANNEL_LABELS":
        return channel_labels()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass
class Attempt:
    """One real attempt on record: ``ok`` True / False / None (outcome unknown), when, and what it was."""

    ok: bool | None
    at: str
    said: str


@dataclass
class LaneFacts:
    """What is true of one lane, before the status word is chosen."""

    configured: bool = False
    # None = the lane needs no authorization; False = a switch is off or a credential is missing (with a sentence).
    authorized: bool | None = None
    attempts: list[Attempt] = field(default_factory=list)  # oldest first
    evidence: list[str] = field(default_factory=list)


@dataclass
class Sources:
    """The facts a projection is computed from, already read. Tests build one by hand; :func:`gather` reads the real
    ones. Every field is plain data so nothing here needs an engine, a socket or the network."""

    plans: dict[str, dict[str, str]] = field(default_factory=dict)
    plans_ready: bool = True
    # The project's orders, newest first: {"created_at", "source"}.
    orders: list[dict[str, str]] = field(default_factory=list)
    # Pager channel id -> wired (every required value set).
    notify_channels: dict[str, bool] = field(default_factory=dict)
    # Pager channel id -> the value problems channel_config_check found, in the owner's words.
    channel_problems: dict[str, list[str]] = field(default_factory=dict)
    channels_muted: bool = False
    # Connector names somebody switched off.
    switches_off: set[str] = field(default_factory=set)
    # API connectors registered for the project: {"connector", "enabled"}.
    registrations: list[dict[str, Any]] = field(default_factory=list)
    project_customer: str = ""
    # Channels the project's customer has a contact identity on.
    contact_channels: list[str] = field(default_factory=list)
    # When Feishu gate cards for the project's orders reached the group (oldest first).
    cards_sent: list[str] = field(default_factory=list)
    # This process's pager sends: {"channel", "ok", "at"} (oldest first). ok None = outcome unknown.
    pager_results: list[dict[str, Any]] = field(default_factory=list)
    # Customer acceptance decisions recorded on the project's orders: {"at", "by"} (oldest first).
    acceptance_decisions: list[dict[str, str]] = field(default_factory=list)
    # When a message about one of the project's orders went out to the customer (oldest first).
    customer_messages: list[str] = field(default_factory=list)
    # What each registered console extension adds to the 客户确认 lane (``CustomerLaneExtra``), already read.
    customer_lane_extras: list[Any] = field(default_factory=list)
    folder: str = ""
    folder_chosen: bool = False
    folder_exists: bool | None = None
    folder_writable: bool | None = None


class CustomerLaneExtra(Protocol):
    """A registered console extension's part of the 客户确认 lane (``extensions.ConsoleExtension.customer_lane``), plain
    data already read: nothing here needs an engine, a socket or the network."""

    # Set up on this project: the lane counts as configured even with no customer bound.
    present: bool
    # Said after 「项目还没有绑定客户」 while it is present and no customer is bound (joined with 「；」).
    unbound_note: str
    # Said when it is present, no customer is bound and nothing was tried yet.
    idle_note: str

    def apply(self, facts: LaneFacts, sources: "Sources") -> list[Attempt]:
        """Mark the lane configured / not authorized with its evidence; return its real attempts (oldest first). They
        are sorted in after the lane's own, so on a tie the extension's line is the one shown."""
        ...


def lane_status(facts: LaneFacts) -> str:
    """The one rule that turns facts into a word. 可用 needs a successful attempt on record; nothing else gives it."""
    if not facts.configured:
        return "unconfigured"
    if facts.authorized is False:
        return "needs_authorization"
    if not facts.attempts:
        return "unverified"
    last = facts.attempts[-1]
    if last.ok is None:
        return "unknown"
    return "usable" if last.ok else "broken"


def _when(value: str) -> str:
    text = str(value or "").strip()
    return text[:16].replace("T", " ") if text else ""


def _channel_of(text: str) -> str:
    lowered = str(text or "").lower()
    for channel, words in _CHANNEL_WORDS.items():
        if any(word in lowered for word in words):
            return channel
    return ""


def _intake(plan: dict[str, str], sources: Sources) -> LaneFacts:
    facts = LaneFacts()
    enabled = [str(row.get("connector", "") or "") for row in sources.registrations if row.get("enabled")]
    if enabled:
        facts.configured = True
        facts.evidence.append("已登记并启用的接口连接器 " + str(len(enabled)) + " 个")
        off = [name for name in enabled if name in sources.switches_off]
        if off:
            facts.authorized = False
            facts.evidence.append("其中 " + str(len(off)) + " 个被关上了，打开后才能用")
    if sources.orders:
        facts.configured = True
        newest = sources.orders[0]
        route = source_words().get(str(newest.get("source", "") or ""), "系统")
        facts.attempts.append(Attempt(True, str(newest.get("created_at", "") or ""),
                                      f"最近一张单 {_when(newest.get('created_at', ''))} 由{route}进来，共 {len(sources.orders)} 张"))
        facts.evidence.append(facts.attempts[-1].said)
    elif facts.configured:
        facts.evidence.append("还没有一张单从这条线路进来过")
    if not facts.configured:
        facts.evidence.append("还没有单子进来过，也没有登记接口连接器" + ("；已规划：" + plan["tool"] if plan.get("tool") else ""))
    return facts


def _review(plan: dict[str, str], sources: Sources) -> LaneFacts:
    facts = LaneFacts()
    channel = _channel_of(plan.get("tool", ""))
    wired = [name for name, ok in sources.notify_channels.items() if ok]
    if plan.get("tool") and not channel:
        facts.evidence.append("规划的软件「" + plan["tool"] + "」控制台还没有接入方式")
        return facts
    if not channel:
        channel = wired[0] if wired else ""
    if not channel or not sources.notify_channels.get(channel):
        facts.evidence.append("还没有配好任何一个通知渠道" if not wired else channel_labels().get(channel, channel) + "还没有配好")
        return facts
    label = channel_labels().get(channel, channel)
    facts.configured = True
    problems = list(sources.channel_problems.get(channel, []) or [])
    facts.evidence.append(label + "已配置" + ("，但有值看起来不对：" + "；".join(problems) if problems else "，值的样子没问题"))
    if sources.channels_muted:
        facts.authorized = False
        facts.evidence.append("外发渠道已静音，打开后才会真的发")
    elif channel in sources.switches_off:
        facts.authorized = False
        facts.evidence.append(label + "的连接器被关上了，打开后才会真的发")
    attempts: list[Attempt] = []
    if channel == "feishu" and sources.cards_sent:
        attempts.append(Attempt(True, sources.cards_sent[-1],
                                f"闸门卡片送达过飞书群 {len(sources.cards_sent)} 次，最近一次 {_when(sources.cards_sent[-1])}"))
    for row in sources.pager_results:
        if str(row.get("channel", "") or "") != channel:
            continue
        ok = row.get("ok")
        outcome = None if ok is None else bool(ok)
        word = "成功" if outcome else ("失败" if outcome is False else "结果不明")
        attempts.append(Attempt(outcome, str(row.get("at", "") or ""),
                                f"本次开机以来的一次{label}通知{word}（{_when(row.get('at', ''))}）"))
    attempts.sort(key=lambda item: item.at)
    facts.attempts = attempts
    if not attempts:
        facts.evidence.append("还没有真发出去过一条：这里只算真发送的记录，测试发送的结果不落库")
    else:
        facts.evidence.append(attempts[-1].said)
    return facts


def _customer(plan: dict[str, str], sources: Sources) -> LaneFacts:
    facts = LaneFacts()
    customer = str(sources.project_customer or "").strip()
    present = [extra for extra in sources.customer_lane_extras if extra.present]
    if not customer and not present:
        facts.evidence.append("项目还没有绑定客户")
        return facts
    channels = [str(item) for item in sources.contact_channels if str(item)]
    labels = channel_labels()
    if not customer:
        facts.evidence.append("；".join(["项目还没有绑定客户", *(extra.unbound_note for extra in present if extra.unbound_note)]))
    elif channels:
        facts.configured = True
        facts.evidence.append("客户在 " + "、".join(labels.get(name, name) for name in dict.fromkeys(channels)) + " 上有联系人")
        if sources.channels_muted:
            facts.authorized = False
            facts.evidence.append("外发渠道已静音，打开后才能联系客户")
        elif any(name in sources.switches_off for name in channels):
            facts.authorized = False
            facts.evidence.append("联系客户用的连接器被关上了")
    else:
        facts.evidence.append("已绑定客户，但还没有登记联系方式")
    extra_attempts = [attempt for extra in sources.customer_lane_extras for attempt in extra.apply(facts, sources)]
    attempts: list[Attempt] = []
    if sources.acceptance_decisions:
        facts.configured = True
        last = sources.acceptance_decisions[-1]
        attempts.append(Attempt(True, str(last.get("at", "") or ""),
                                f"客户验收记录过 {len(sources.acceptance_decisions)} 次，最近一次 {_when(last.get('at', ''))}"))
    if sources.customer_messages:
        attempts.append(Attempt(True, sources.customer_messages[-1],
                                f"向客户发出过 {len(sources.customer_messages)} 条消息，最近一次 {_when(sources.customer_messages[-1])}"))
    # The extensions' attempts after the general ones: a customer's 确认 recorded through an extension is also an
    # acceptance decision at the same moment, and on a tie (the sort is stable) the line that says where it came from
    # is the one shown.
    attempts.extend(extra_attempts)
    attempts.sort(key=lambda item: item.at)
    facts.attempts = attempts
    if facts.configured and not attempts:
        idle = next((extra.idle_note for extra in present if extra.idle_note), "") if not customer else ""
        facts.evidence.append(idle or "还没有一次记录在案的客户确认")
    elif attempts:
        facts.evidence.append(attempts[-1].said)
    return facts


def _files(plan: dict[str, str], sources: Sources) -> LaneFacts:
    facts = LaneFacts()
    if not sources.folder:
        facts.evidence.append("还不知道这个项目的文件放在哪")
        return facts
    if sources.folder_chosen:
        facts.configured = True
        facts.evidence.append("交付目录由你指定")
    elif sources.folder_exists:
        facts.configured = True
        facts.evidence.append("交付目录在默认位置")
    else:
        facts.evidence.append("还没有指定交付目录，也还没有文件落到默认位置")
        return facts
    if sources.folder_exists is False:
        facts.attempts.append(Attempt(False, "", "目录现在找不到（刚核对）"))
    elif sources.folder_exists and sources.folder_writable is False:
        facts.attempts.append(Attempt(False, "", "目录在，但现在写不进去（刚核对）"))
    elif sources.folder_exists and sources.folder_writable:
        facts.attempts.append(Attempt(True, "", "目录在，可写（刚核对）"))
    if facts.attempts:
        facts.evidence.append(facts.attempts[-1].said)
    return facts


_LANE_FACTS = {"intake": _intake, "review": _review, "customer": _customer, "files": _files}


def project_connections(project_id: str, sources: Sources) -> list[dict[str, Any]]:
    """The four lanes of one project, each with the owner's plan, a status code, its word and the evidence."""
    lanes: list[dict[str, Any]] = []
    for lane in CONNECTION_LANES:
        plan = {name: str(dict(sources.plans.get(lane, {}) or {}).get(name, "") or "") for name in CONNECTION_PLAN_FIELDS}
        facts = _LANE_FACTS[lane](plan, sources)
        status = lane_status(facts)
        lanes.append({
            "lane": lane,
            "label": CONNECTION_LANE_LABELS[lane],
            **plan,
            "status": status,
            "status_word": STATUS_WORDS[status],
            "evidence": list(dict.fromkeys(item for item in facts.evidence if item)),
        })
    return lanes


def summary(lanes: list[dict[str, Any]]) -> dict[str, str]:
    """``{lane: status}`` for the project list."""
    return {str(item["lane"]): str(item["status"]) for item in lanes}


def gather(project_id: str, *, business: Any, folder: Path | str | None, folder_chosen: bool,
           duty_log: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
           registrations: list[dict[str, Any]] | tuple[dict[str, Any], ...] = ()) -> Sources:
    """Read the real facts for one project. Nothing is sent, nothing is written; the folder is only probed."""
    from ampuly.plugins.office_ui.notifier import channel_config_check, channels_muted, notify_status

    clean = str(project_id or "").strip()
    sources = Sources(plans=dict(business.project_connection_plans(clean)), plans_ready=business.project_center_ready())
    orders = list(business.list_orders(project_id=clean, limit=_ACCEPTANCE_SAMPLE))
    sources.orders = [{"created_at": str(order.created_at or ""), "source": str(order.source or "")} for order in orders]
    sources.notify_channels = {str(row["id"]): bool(row.get("configured")) for row in notify_status()["channels"]}
    sources.channel_problems = {
        str(row["id"]): [
            str(spec.get("label_zh", "") or "") + "：" + str(spec.get("detail_zh", "") or "")
            for spec in list(row.get("fields", []) or []) if spec.get("detail_zh")
        ]
        for row in channel_config_check()["channels"]
    }
    sources.channels_muted = channels_muted()
    sources.switches_off = {
        str(row["connector"]) for row in business.list_connector_switches() if not row.get("enabled", True)
    }
    sources.registrations = [{"connector": str(row.get("connector", "") or ""), "enabled": bool(row.get("enabled"))}
                             for row in registrations]
    customer = str(business.project_customer(clean) or "")
    sources.project_customer = customer
    if customer:
        sources.contact_channels = [
            str(identity.get("channel", "") or "")
            for contact in business.list_contacts(customer_ids=(customer,))
            for identity in list(contact.get("identities", []) or [])
        ]
    # A registered console extension's part of the 客户确认 lane: what it has on this project and what it tried.
    from ampuly.plugins.office_ui import extensions

    for _name, read in extensions.customer_lane_readers():
        try:
            extra = read(clean, business)
        except Exception:  # noqa: BLE001 - unreadable: the lane reads as if the extension were not there
            continue
        if extra is not None:
            sources.customer_lane_extras.append(extra)
    cards: list[str] = []
    decisions: list[dict[str, str]] = []
    for order in orders:
        for card in business.gate_cards_for_order(order.order_id):
            if str(card.get("message_id", "") or ""):
                cards.append(str(card.get("sent_at", "") or ""))
        try:
            record = business.acceptance(order.order_id, project_id=clean)
        except Exception:  # noqa: BLE001 - an order gone between the two reads is no reason to fail the page
            record = None
        if record is not None and record.decided_at:
            decisions.append({"at": str(record.decided_at), "by": str(record.decided_by or "")})
    sources.cards_sent = sorted(cards)
    sources.acceptance_decisions = sorted(decisions, key=lambda item: item["at"])
    sources.customer_messages = sorted(business.outbound_message_times(order.order_id for order in orders))
    sources.pager_results = [
        {"channel": str(sent.get("channel", "") or ""), "ok": sent.get("ok"), "at": str(entry.get("at", "") or "")}
        for entry in duty_log
        if str(entry.get("project_id", "") or "").strip() == clean
        for sent in list(entry.get("sent", []) or [])
    ]
    if folder:
        target = Path(folder)
        sources.folder = str(target)
        sources.folder_chosen = bool(folder_chosen)
        try:
            exists = target.is_dir()
            sources.folder_exists = exists
            sources.folder_writable = os.access(str(target), os.W_OK) if exists else None
        except OSError:
            sources.folder_exists = None
            sources.folder_writable = None
    return sources
