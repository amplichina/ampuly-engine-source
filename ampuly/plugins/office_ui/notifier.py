"""Outbound-only owner notifications over Feishu and DingTalk.

The runtime ships eleven channel adapters, all built for two-way chat and
none ever started by ``ampuly ui``. What the owner actually needs from a
channel is one thing: a private message when a run wants a decision, has
finished, or has stalled — "call the person, never approve in a group".
So this is a sender, not a bot: no inbound listener, no allow-lists, no
session mapping. Credentials come from the environment (never a file),
targets are user ids, and every send is best-effort — a notification that
fails must never take the run down with it.

Plugin-layer module: no engine-kernel changes.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger

from ampuly.i18n_runtime import is_zh
from ampuly.plugins.office_ui.product_name import product_name

# Environment names. Kept apart from the upstream channel_config.yaml keys on
# purpose: that file is for the two-way channel runtime, these are for the
# duty officer's pager.
FEISHU_APP_ID = "AMPULY_NOTIFY_FEISHU_APP_ID"
FEISHU_APP_SECRET = "AMPULY_NOTIFY_FEISHU_APP_SECRET"
FEISHU_TO = "AMPULY_NOTIFY_FEISHU_TO"
DINGTALK_CLIENT_ID = "AMPULY_NOTIFY_DINGTALK_CLIENT_ID"
DINGTALK_CLIENT_SECRET = "AMPULY_NOTIFY_DINGTALK_CLIENT_SECRET"
DINGTALK_TO = "AMPULY_NOTIFY_DINGTALK_TO"

# A launch-time mute for every outbound channel (duty pager, gate cards, the
# Feishu long connection).  Read from the *process* environment only -- never
# from the user registry that the settings page writes -- so a console started
# for a local smoke or a sales demo stays silent no matter which credentials
# are stored on the machine.  Muting is per process: it ends when the process
# does and cannot be flipped on from the settings page.
CHANNELS_MUTED_ENV = "AMPULY_CHANNELS_MUTED"

NOTIFY_ENV_NAMES = (
    FEISHU_APP_ID,
    FEISHU_APP_SECRET,
    FEISHU_TO,
    DINGTALK_CLIENT_ID,
    DINGTALK_CLIENT_SECRET,
    DINGTALK_TO,
    # human gates in the Feishu group (the module contract)
    "AMPULY_FEISHU_GATE_CHAT",
    "AMPULY_FEISHU_OWNER_OPEN_ID",
    # A registered console extension's own key-storage variables (``extensions.secret_env_names``) are allowed beside
    # these by ``settings_service``; they are defined once, in the extension.
)

# The settings page renders these; secrets are masked there like API keys.
NOTIFY_CHANNELS: list[dict[str, Any]] = [
    {
        "id": "feishu",
        "name_zh": "飞书",
        "name_en": "Feishu",
        "fields": [
            {"env": FEISHU_APP_ID, "label_zh": "App ID", "label_en": "App ID", "secret": False},
            {"env": FEISHU_APP_SECRET, "label_zh": "App Secret", "label_en": "App Secret", "secret": True},
            {
                "env": FEISHU_TO,
                "label_zh": "收件人 open_id（或 oc_ 开头的群 ID）",
                "label_en": "Recipient open_id (or an oc_ chat id)",
                "secret": False,
            },
            {
                "env": "AMPULY_FEISHU_GATE_CHAT",
                "label_zh": "工单群 chat_id（oc_ 开头；闸门卡片发到这里，空则用上面的收件人）",
                "label_en": "Gate group chat_id (oc_...; cards go here, falls back to recipient)",
                "secret": False,
                "optional": True,
            },
            {
                "env": "AMPULY_FEISHU_OWNER_OPEN_ID",
                "label_zh": "老板 open_id（ou_ 开头；在群里能放行任何节点）",
                "label_en": "Owner open_id (ou_...; may approve any node from the group)",
                "secret": False,
                "optional": True,
            },
        ],
        "note_zh": "在飞书开放平台建一个企业自建应用，开通「以应用身份发消息」权限；收件人填你自己的 open_id，默认只私聊你，不进群。",
        "note_en": "Create an internal app on the Feishu open platform with the send-message scope; put your own open_id as recipient — it DMs you, never a group.",
    },
    {
        "id": "dingtalk",
        "name_zh": "钉钉",
        "name_en": "DingTalk",
        "fields": [
            {"env": DINGTALK_CLIENT_ID, "label_zh": "Client ID (AppKey)", "label_en": "Client ID (AppKey)", "secret": False},
            {"env": DINGTALK_CLIENT_SECRET, "label_zh": "Client Secret", "label_en": "Client Secret", "secret": True},
            {"env": DINGTALK_TO, "label_zh": "收件人 userId", "label_en": "Recipient userId", "secret": False},
        ],
        "note_zh": "钉钉开放平台建企业内部应用并添加机器人；收件人填你在企业内的 userId，走机器人单聊。",
        "note_en": "Create an internal DingTalk app with a robot; the recipient is your userId inside the org — robot DM only.",
    },
]


def channels_muted() -> bool:
    """True when this process was started with AMPULY_CHANNELS_MUTED=1 (or true/yes/on)."""
    return (os.environ.get(CHANNELS_MUTED_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def require_outbound_enabled(channel: str, check: Any = None) -> None:
    """Recheck the live switch, including for already-created HTTP clients."""
    if channels_muted():
        raise RuntimeError("Outbound channels are muted")
    if check is not None:
        try:
            refused = check(channel)
        except Exception:
            raise RuntimeError("Outbound switch unavailable; request not sent") from None
        if refused:
            raise RuntimeError(f"{channel} outbound disabled; request not sent")


def _env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if value or os.name != "nt":
        return value
    # A value set from the settings page lands in the user registry, not in
    # this process; look there so a restart is not required to pick it up.
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0]).strip()
    except OSError:
        return ""


@dataclass
class SendResult:
    channel: str
    ok: bool
    detail: str = ""


class FeishuSender:
    """Tenant token + im/v1 message, the same two calls the adapter makes."""

    name = "feishu"
    outbound_allowed: Any = None

    def __init__(self, app_id: str, app_secret: str, to: str, *, http: httpx.AsyncClient | None = None) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.to = to
        self._http = http
        self._token = ""
        self._token_expiry = 0.0

    async def _client(self) -> httpx.AsyncClient:
        require_outbound_enabled(self.name, self.outbound_allowed)
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15.0)
        return self._http

    async def _tenant_token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        http = await self._client()
        resp = await http.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        if int(data.get("code", 0) or 0) != 0:
            raise RuntimeError(f"feishu token: {data.get('msg') or data.get('code')}")
        self._token = str(data.get("tenant_access_token", "") or "")
        self._token_expiry = time.time() + int(data.get("expire", 7200) or 7200) - 60
        return self._token

    async def send(self, text: str) -> SendResult:
        try:
            token = await self._tenant_token()
            http = await self._client()
            receive_id_type = "chat_id" if self.to.startswith("oc_") else "open_id"
            resp = await http.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": self.to, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
            )
            resp.raise_for_status()
            data = resp.json()
            if int(data.get("code", 0) or 0) != 0:
                return SendResult(self.name, False, str(data.get("msg") or data.get("code")))
            return SendResult(self.name, True)
        except Exception as exc:  # a pager that fails must not take the run down
            return SendResult(self.name, False, f"{type(exc).__name__}: {exc}")


class DingTalkSender:
    """OAuth token + robot oToMessages batchSend, as in the adapter."""

    name = "dingtalk"
    outbound_allowed: Any = None

    def __init__(self, client_id: str, client_secret: str, to: str, *, http: httpx.AsyncClient | None = None) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.to = to
        self._http = http
        self._token = ""
        self._token_expiry = 0.0

    async def _client(self) -> httpx.AsyncClient:
        require_outbound_enabled(self.name, self.outbound_allowed)
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15.0)
        return self._http

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        http = await self._client()
        resp = await http.post(
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            json={"appKey": self.client_id, "appSecret": self.client_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = str(data.get("accessToken", "") or "")
        if not self._token:
            raise RuntimeError(f"dingtalk token: {data}")
        self._token_expiry = time.time() + int(data.get("expireIn", 7200) or 7200) - 60
        return self._token

    async def send(self, text: str) -> SendResult:
        try:
            token = await self._access_token()
            http = await self._client()
            resp = await http.post(
                "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                headers={"x-acs-dingtalk-access-token": token},
                json={
                    "robotCode": self.client_id,
                    "userIds": [self.to],
                    "msgKey": "sampleMarkdown",
                    "msgParam": json.dumps({"title": product_name(), "text": text}, ensure_ascii=False),
                },
            )
            resp.raise_for_status()
            return SendResult(self.name, True)
        except Exception as exc:
            return SendResult(self.name, False, f"{type(exc).__name__}: {exc}")


@dataclass
class Notifier:
    senders: list[Any] = field(default_factory=list)

    @property
    def channels(self) -> list[str]:
        return [sender.name for sender in self.senders]

    @property
    def configured(self) -> bool:
        return bool(self.senders)

    async def send(self, text: str) -> list[SendResult]:
        results: list[SendResult] = []
        for sender in self.senders:
            result = await sender.send(text)
            if not result.ok:
                logger.warning(f"notify: {result.channel} failed: {result.detail}")
            results.append(result)
        return results


def notifier_from_env(*, http: httpx.AsyncClient | None = None, outbound_allowed: Any = None) -> Notifier:
    """Build senders for every channel whose credentials and target are set.

    A muted process gets a notifier with no senders at all, so every caller
    that asks ``notifier.configured`` or calls ``send`` sees the same thing:
    nothing leaves this machine.
    """
    senders: list[Any] = []
    if channels_muted():
        return Notifier(senders)
    if _env(FEISHU_APP_ID) and _env(FEISHU_APP_SECRET) and _env(FEISHU_TO):
        senders.append(FeishuSender(_env(FEISHU_APP_ID), _env(FEISHU_APP_SECRET), _env(FEISHU_TO), http=http))
    if _env(DINGTALK_CLIENT_ID) and _env(DINGTALK_CLIENT_SECRET) and _env(DINGTALK_TO):
        senders.append(DingTalkSender(_env(DINGTALK_CLIENT_ID), _env(DINGTALK_CLIENT_SECRET), _env(DINGTALK_TO), http=http))
    for sender in senders:
        sender.outbound_allowed = outbound_allowed
    return Notifier(senders)


# What a correctly pasted value looks like, per field.  Shape only: this
# never reports a value, and nothing here touches the network.
_VALUE_SHAPES: dict[str, tuple[str, str, str]] = {
    FEISHU_APP_ID: (r"^cli_[A-Za-z0-9]+$", "以 cli_ 开头的 App ID", "an app id starting with cli_"),
    FEISHU_TO: (r"^(ou_|oc_|on_)[A-Za-z0-9_-]+$", "ou_ 开头的 open_id 或 oc_ 开头的群 ID", "an ou_ open_id or an oc_ chat id"),
    "AMPULY_FEISHU_GATE_CHAT": (r"^oc_[A-Za-z0-9_-]+$", "oc_ 开头的群 ID", "an oc_ chat id"),
    "AMPULY_FEISHU_OWNER_OPEN_ID": (r"^ou_[A-Za-z0-9_-]+$", "ou_ 开头的 open_id", "an ou_ open_id"),
    DINGTALK_CLIENT_ID: (r"^[A-Za-z0-9_-]{8,}$", "AppKey", "an AppKey"),
}


def channel_config_check() -> dict[str, Any]:
    """Read-only verdict on the channel credentials in this process.

    The mistakes that actually happen are paste mistakes: a chat id in the
    open_id field, a value that carried its quotes or a trailing space in from
    a shell, a secret that never made it into the environment at all. Each
    costs a failed send to discover, and a failed send is discovered in front
    of a customer.

    So this checks *shape*, never content: no value is returned, nothing is
    sent, no token is fetched. A channel that looks right here has not been
    proved to work -- only proved not to be obviously wrong.
    """
    import re

    channels: list[dict[str, Any]] = []
    for channel in NOTIFY_CHANNELS:
        problems: list[dict[str, str]] = []
        fields: list[dict[str, Any]] = []
        for spec in channel["fields"]:
            env_name = str(spec["env"])
            raw = os.environ.get(env_name)
            value = (raw or "")
            trimmed = value.strip()
            state = "unset"
            detail_zh, detail_en = "", ""
            if not trimmed:
                state = "unset"
                if not spec.get("optional"):
                    detail_zh, detail_en = "必填项还没设", "required and not set"
            elif value != trimmed:
                state = "padded"
                detail_zh, detail_en = "两端有空白，多半是粘贴带进来的", "has surrounding whitespace, most likely pasted in"
            elif len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in "\"'":
                state = "quoted"
                detail_zh, detail_en = "值把引号也带上了", "the quotes were stored as part of the value"
            else:
                shape = _VALUE_SHAPES.get(env_name)
                if shape and not re.match(shape[0], trimmed):
                    state = "unexpected_shape"
                    detail_zh = f"看起来不像{shape[1]}"
                    detail_en = f"does not look like {shape[2]}"
                else:
                    state = "ok"
            entry = {
                "env": env_name,
                "label_zh": spec["label_zh"],
                "label_en": spec["label_en"],
                "optional": bool(spec.get("optional")),
                "state": state,
            }
            if detail_zh or detail_en:
                entry["detail_zh"] = detail_zh
                entry["detail_en"] = detail_en
                problems.append({"env": env_name, "state": state})
            fields.append(entry)
        channels.append({
            "id": channel["id"],
            "name_zh": channel["name_zh"],
            "name_en": channel["name_en"],
            "fields": fields,
            "problems": len(problems),
            # "Ready" means nothing here is obviously wrong. It is not a claim
            # that a message would arrive; only a real send proves that.
            "shape_ok": not problems,
        })
    return {
        "muted": channels_muted(),
        "channels": channels,
        "checked_network": False,
    }


def notify_status() -> dict[str, Any]:
    """What the settings page shows: which channels are wired, never the values."""
    return {
        "muted": channels_muted(),
        "channels": [
            {
                "id": channel["id"],
                "name_zh": channel["name_zh"],
                "name_en": channel["name_en"],
                "note_zh": channel["note_zh"],
                "note_en": channel["note_en"],
                "configured": all(_env(f["env"]) for f in channel["fields"] if not f.get("optional")),
                "fields": [
                    {
                        "env": f["env"],
                        "label_zh": f["label_zh"],
                        "label_en": f["label_en"],
                        "secret": f["secret"],
                        "set": bool(_env(f["env"])),
                        "value": ("" if f["secret"] else _env(f["env"])),
                    }
                    for f in channel["fields"]
                ],
            }
            for channel in NOTIFY_CHANNELS
        ]
    }


# ── Message composition ───────────────────────────────────────────────────


# Owner's rules ( / ): every message says the product's own name, and no message in Feishu or
# DingTalk sends the owner to the console -- decisions, acceptance and closing are all made on the Feishu card.  The
# ``base_url`` parameters stay for the callers; no message text uses them.


def _tag() -> str:
    return f"【{product_name(True)}】" if is_zh() else f"[{product_name(False)}] "


def compose_waiting(order_title: str, subject: str, waiting_for: str, *, base_url: str) -> str:
    """The duty officer's nudge for a pause.  No console address: every decision is made on the Feishu card (owner's
    rule, 2026-09-25: nothing in the group sends the owner out of Feishu).  ``base_url`` is kept for the callers."""
    del base_url
    if is_zh():
        what = f"「{subject}」" if subject else "一个事项"
        wait = f"（{waiting_for}）" if waiting_for else ""
        return (
            f"{_tag()}订单「{order_title}」在等你拍板：{what}{wait}。"
            f"所有决定都在飞书卡片上做：在卡片上查看材料，直接点放行或打回。"
        )
    what = f"'{subject}'" if subject else "a checkpoint"
    wait = f" ({waiting_for})" if waiting_for else ""
    return (
        f"{_tag()}Order '{order_title}' is waiting on you: {what}{wait}. "
        f"Every decision is made on the Feishu card: review the material there and approve or send it back on the card."
    )


# How an order that ended without a delivery is said; an unknown status falls back to the raw word.
_ENDED_ZH = {"failed": "没有做成，已经停下", "cancelled": "已取消"}
_ENDED_EN = {"failed": "failed and has stopped", "cancelled": "was cancelled"}


def compose_finished(order_title: str, status: str, *, base_url: str) -> str:
    del base_url
    good = status in {"done", "delivered"}
    if is_zh():
        if good:
            return f"{_tag()}订单「{order_title}」已完成交付，可以验收。验收和结案都在飞书卡片上做。"
        ended = _ENDED_ZH.get(status) or f"已结束（{status}）"
        return f"{_tag()}订单「{order_title}」{ended}。"
    if good:
        return (
            f"{_tag()}Order '{order_title}' has been delivered and is ready for your acceptance. "
            f"Acceptance and closing are both done on the Feishu card."
        )
    ended = _ENDED_EN.get(status) or f"ended with status {status}"
    return f"{_tag()}Order '{order_title}' {ended}."


def compose_stalled(order_title: str, since_last_tool: str, last_tool: str, *, base_url: str) -> str:
    del base_url
    if is_zh():
        if last_tool:
            last = f"最后一次动作是 {last_tool}（{since_last_tool}）" if since_last_tool else f"最后一次动作是 {last_tool}"
        else:
            last = f"上次动作在 {since_last_tool}" if since_last_tool else "很久没有新动作"
        return (
            f"{_tag()}订单「{order_title}」可能悬死：没有在等任何人，也没有模型在思考，{last}。"
            f"值守会继续看着；需要你决定的事会以飞书卡片出现。"
        )
    if last_tool:
        last = f"last action was {last_tool} ({since_last_tool})" if since_last_tool else f"last action was {last_tool}"
    else:
        last = f"last action {since_last_tool}" if since_last_tool else "no new action for a long time"
    return (
        f"{_tag()}Order '{order_title}' looks stalled: nothing is waiting on a person, no model call is in flight, "
        f"{last}. The duty officer keeps watching; anything that needs your decision will arrive as a Feishu card."
    )


def compose_test(*, base_url: str) -> str:
    del base_url
    return (
        f"{_tag()}通道测试消息：收到这条说明值班员可以联系到你。"
        if is_zh()
        else f"{_tag()}Test message: the duty officer can reach you."
    )
