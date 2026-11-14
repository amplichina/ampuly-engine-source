from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from loguru import logger

from ampuly.channels.provider_base import OptionalDependencyChannel
from ampuly.core.models import SystemMessage


class FeishuChannel(OptionalDependencyChannel):
    name = "feishu"
    required_package = "lark_oapi"
    delivery_mode = "socket"

    _ENV_FIELDS = ("app_id", "app_secret", "encrypt_key", "verification_token")

    @classmethod
    def _resolve_env_credentials(cls, config: Any) -> Any:
        """Fill the credential fields from the variables the config names.

        The YAML keeps the variable name; the value lives in the process
        environment. A named variable that is missing raises with the name
        only, so the refusal can be read without the secret ever being.
        """
        import os

        names = {field: str(getattr(config, f"{field}_env", "") or "").strip() for field in cls._ENV_FIELDS}
        if not any(names.values()):
            return config
        values: dict[str, str] = {}
        missing: list[str] = []
        for field, env_name in names.items():
            if not env_name or str(getattr(config, field, "") or "").strip():
                continue
            value = os.environ.get(env_name, "")
            if not value:
                missing.append(env_name)
            else:
                values[field] = value
        if missing:
            raise ValueError(f"feishu channel: environment variable(s) not set: {', '.join(missing)}")
        copier = getattr(config, "model_copy", None)
        return copier(update=values) if callable(copier) else config

    def __init__(self, config: Any, bus: Any):
        super().__init__(self._resolve_env_credentials(config), bus)
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def get_required_config_fields(self) -> list[str]:
        return ["app_id", "app_secret"]

    def account_identity(self) -> str:
        return str(getattr(self.config, "app_id", "") or "").strip()

    def normalize_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = payload.get("event", payload)
        sender = (event.get("sender") or {}).get("sender_id", {})
        message = event.get("message", {})
        raw_content = message.get("content", {})
        if isinstance(raw_content, str):
            try:
                raw_content = json.loads(raw_content)
            except json.JSONDecodeError:
                raw_content = {"text": raw_content}
        sender_id = str(sender.get("open_id", "") or "")
        chat_id = str(message.get("chat_id", "") or "")
        if str(message.get("chat_type", "") or "") == "p2p":
            chat_id = sender_id
        msg_type = str(message.get("message_type", "") or "").strip().lower()
        content = str(raw_content.get("text", "") or raw_content.get("content", "") or "")
        attachments: list[dict] = []
        # Feishu sends image/file as a separate event from the caption. Surface
        # them as attachments so the optional coalesce window can merge them.
        if msg_type in {"image", "file", "audio", "media", "video", "sticker"}:
            image_key = str(raw_content.get("image_key") or raw_content.get("file_key") or "").strip()
            attachments.append(
                {
                    "type": msg_type,
                    "image_key": image_key,
                    "file_key": str(raw_content.get("file_key") or "").strip(),
                    "file_name": str(raw_content.get("file_name") or "").strip(),
                }
            )
            if not content.strip():
                content = ""
        return {
            "sender_id": sender_id,
            "chat_id": chat_id,
            "content": content,
            "attachments": attachments,
            "thread_id": str(message.get("thread_id", "") or ""),
            "reply_to": str(message.get("parent_id", "") or ""),
            "metadata": {
                "message_id": str(message.get("message_id", "") or ""),
                "chat_type": str(message.get("chat_type", "") or ""),
                "message_type": msg_type,
            },
        }

    async def start(self) -> None:
        await super().start()
        import lark_oapi as lark
        import lark_oapi.ws.client as lark_ws_client

        self._loop = asyncio.get_running_loop()
        self._client = lark.Client.builder().app_id(self.config.app_id).app_secret(self.config.app_secret).build()
        event_handler = (
            lark.EventDispatcherHandler.builder(self.config.encrypt_key or "", self.config.verification_token or "")
            .register_p2_im_message_receive_v1(self._on_message_sync)
            .build()
        )
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            # INFO logs the credential-bearing WebSocket endpoint URL.
            log_level=lark.LogLevel.WARNING,
        )

        def _run_ws() -> None:
            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            lark_ws_client.loop = ws_loop
            try:
                while self.is_running:
                    try:
                        self._ws_client.start()
                    except Exception as exc:
                        self.set_last_error(exc)
                        if self.is_running:
                            import time

                            time.sleep(5)
            finally:
                ws_loop.close()

        self._ws_thread = threading.Thread(target=_run_ws, daemon=True)
        self._ws_thread.start()

    async def stop(self) -> None:
        self.mark_stopped()
        await super().stop()

    def _on_message_sync(self, data: Any) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    async def _on_message(self, data: Any) -> None:
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        if message is None or sender is None:
            return
        message_id = str(getattr(message, "message_id", "") or "")
        # Said at INFO because silence here is indistinguishable from "the
        # event never arrived" -- the live check of 2026-09-09 spent its time
        # on exactly that question. Identities are masked; content is not logged.
        sender_open_id = str(getattr(getattr(sender, "sender_id", None), "open_id", "") or "")
        logger.info(
            "feishu inbound: message {} from {}… chat_type={}",
            message_id[-6:] or "?", sender_open_id[:6], str(getattr(message, "chat_type", "") or ""),
        )
        content = getattr(message, "content", "") or ""
        try:
            content_json = json.loads(content) if isinstance(content, str) and content else {}
        except json.JSONDecodeError:
            content_json = {"text": content}
        payload = {
            "event": {
                "sender": {"sender_id": {"open_id": getattr(getattr(sender, "sender_id", None), "open_id", "")}},
                "message": {
                    "message_id": message_id,
                    "chat_id": getattr(message, "chat_id", ""),
                    "chat_type": getattr(message, "chat_type", ""),
                    "message_type": getattr(message, "message_type", ""),
                    "content": content_json,
                    "thread_id": getattr(message, "thread_id", ""),
                    "parent_id": getattr(message, "parent_id", ""),
                },
            }
        }
        # A redelivery after a restart is the common case, not the corner:
        # the restart is why the acknowledgement never arrived. That
        # decision, and the allow list, now come from the shared door.
        normalized = self.normalize_event(payload)
        decision = self.decide_inbound(normalized, message_id=message_id)
        if decision.duplicate:
            logger.info("feishu inbound: message {} already handled; ignored", message_id[-6:] or "?")
            return
        if decision.accepted:
            await self.publish_normalized(normalized)
        else:
            logger.info(
                "feishu inbound: message {} not accepted ({})",
                message_id[-6:] or "?", decision.reason or "refused",
            )
        # The reaction says "seen", not "accepted", and it said that
        # before this change for a refused sender too.
        if message_id and self.config.react_emoji:
            await asyncio.to_thread(self._add_reaction_sync, message_id, self.config.react_emoji)

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        if self._client is None:
            return
        try:
            from lark_oapi.api.im.v1 import CreateMessageReactionRequest, CreateMessageReactionRequestBody, Emoji

            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )
            self._client.im.v1.message_reaction.create(request)
        except Exception:
            logger.debug("feishu reaction add failed")

    def _send_message_sync(self, receive_id_type: str, receive_id: str, msg_type: str, content: str) -> None:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(f"feishu send failed: {response.code} {response.msg}")

    async def send(self, message: SystemMessage) -> None:
        await super().send(message)
        if self._client is None:
            logger.warning("feishu client not connected")
            return
        metadata = dict(message.metadata or {})
        chat_id = str(metadata.get("chat_id") or message.session_id or "")
        receive_id_type = "chat_id" if chat_id.startswith("oc_") else "open_id"
        if message.content.strip():
            body = json.dumps({"text": message.content.strip()}, ensure_ascii=False)
            await asyncio.to_thread(self._send_message_sync, receive_id_type, chat_id, "text", body)
