"""A channel that delivers into a directory, so sending can be built offline.

Every channel this system has needs credentials, a group to post into, and a
person to look at it. That makes the whole path around sending -- proposing an
effect, approving it, applying it once and not twice, verifying the receipt,
routing a reply back onto the order -- expensive to develop and impossible to
assert on.

This channel implements the same surface the real ones do and writes each
outbound message to a file instead. The production effect gateway, the
approval flow and the channel provider all run unchanged; only the last inch
lands on disk. Inbound is symmetrical: a fixture message goes through the same
boundary that stamps ``untrusted_external`` and screens what it is trying to
do, so nothing about inbound is special-cased for tests either.

It cannot be mistaken for a real channel: its name is its own, it holds no
credential, and it refuses to run outside test mode.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from ampuly.channels.base import BaseChannel
from ampuly.llm.deterministic import TEST_MODE_ENV, TestModeRefused, test_mode_enabled

CHANNEL_NAME = "local_inbox"


class LocalInboxChannel(BaseChannel):
    """Delivers to ``<root>/outbox`` and reads fixtures from ``<root>/inbox``."""

    name = CHANNEL_NAME

    def __init__(self, config: Any, bus: Any, root: Path | str) -> None:
        if not test_mode_enabled():
            raise TestModeRefused(f"the local inbox channel needs {TEST_MODE_ENV}=1")
        super().__init__(config, bus)
        self.root = Path(root)
        self.outbox = self.root / "outbox"
        self.inbox = self.root / "inbox"
        self.sent: list[dict[str, Any]] = []

    async def start(self) -> None:
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.mark_started()

    async def stop(self) -> None:
        self.mark_stopped()

    async def send(self, message: Any) -> None:
        """Write the message where a person could read it, and record a receipt.

        A real channel's send can fail after the remote side already acted, so
        this one keeps the same shape: the receipt is written first and the
        caller learns the delivery id, rather than a boolean that hides which
        message actually landed.
        """
        if not self.is_running:
            raise RuntimeError("local inbox channel is not running")
        delivery_id = uuid.uuid4().hex[:12]
        record = {
            "delivery_id": delivery_id,
            "at": datetime.now().isoformat(timespec="seconds"),
            "channel": self.name,
            "user_id": str(getattr(message, "user_id", "") or ""),
            "session_id": str(getattr(message, "session_id", "") or ""),
            "content": str(getattr(message, "content", "") or ""),
            "message_type": str(getattr(message, "message_type", "") or ""),
            "actions": list(getattr(message, "actions", []) or []),
            "task_ref": getattr(message, "task_ref", None),
            "metadata": dict(getattr(message, "metadata", {}) or {}),
        }
        path = self.outbox / f"{record['at'].replace(':', '')}-{delivery_id}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        record["path"] = str(path)
        self.sent.append(record)
        self.last_outbound = dict(record)

    # ── inbound, through the same boundary the real channels use ──

    async def deliver_inbound(
        self,
        *,
        sender_id: str,
        chat_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Feed one fixture message in as if it had arrived from outside.

        Through the same door a real channel uses, so a fixture exercises
        the deduplication rather than skipping past it.
        """
        meta = dict(metadata or {})
        message_id = str(meta.get("message_id", "") or "")
        if message_id:
            decision = self.decide_inbound(
                {"sender_id": sender_id, "content": content, "metadata": meta},
                message_id=message_id,
            )
            if not decision.accepted:
                logger.info(
                    "local_inbox: message {} not accepted ({})",
                    message_id, decision.reason or "refused",
                )
                return
        await self.publish_inbound(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            metadata=meta,
        )

    def read_outbox(self) -> list[dict[str, Any]]:
        """Every delivery on disk, oldest first -- the durable record."""
        out: list[dict[str, Any]] = []
        if not self.outbox.is_dir():
            return out
        for path in sorted(self.outbox.glob("*.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    def is_allowed(self, sender_id: str) -> bool:
        """Fixtures come from the test itself; the allowlist is not the subject here."""
        return bool(str(sender_id or "").strip())
