"""First production effect: send one message through an existing channel."""

from __future__ import annotations

from typing import Any, Callable

from ampuly.core.models import SystemMessage

from .models import (
    EffectRecord,
    ProviderOutcome,
    ProviderResult,
    SimulationStatus,
    VerificationStatus,
)


ENTERPRISE_MESSAGE_SEND = "enterprise_message.send"


class ChannelMessageEffectProvider:
    """Wrap a BaseChannel-like object without giving the Agent channel secrets."""

    supports_idempotency = False

    def __init__(
        self,
        channel: Any,
        *,
        credential_check: Callable[[Any], str] | None = None,
    ) -> None:
        self.channel = channel
        self.name = f"channel:{str(getattr(channel, 'name', '') or '').strip()}"
        if self.name == "channel:":
            raise ValueError("channel name is required")
        # Asked whether this customer's message may go out over this channel's
        # account. Injected rather than imported: which account belongs to
        # which customer is a business question, and this layer should not
        # learn to answer it. Absent, nothing changes.
        self._credential_check = credential_check

    def bind_credential_check(self, check: Callable[[Any], str] | None) -> None:
        """Late-bind the question, because the answer is not known this early.

        The provider is built when the engine wires its channels; who owns
        which account is a business fact the office layer holds. Binding after
        the fact keeps the import pointing one way.
        """
        self._credential_check = check

    def _credential_refusal(self, effect: EffectRecord) -> str:
        if self._credential_check is None:
            return ""
        try:
            return str(self._credential_check(effect) or "").strip()
        except Exception as exc:  # noqa: BLE001 - an unanswerable question is still a refusal
            return f"could not establish which account this customer's messages go out under: {type(exc).__name__}"

    async def simulate(
        self, effect: EffectRecord
    ) -> tuple[SimulationStatus, dict[str, Any]]:
        payload = effect.payload
        missing = [
            field
            for field in ("channel", "user_id", "session_id", "content")
            if not str(payload.get(field) or "").strip()
        ]
        channel_name = str(payload.get("channel") or "").strip()
        expected_name = str(getattr(self.channel, "name", "") or "").strip()
        refusal = self._credential_refusal(effect)
        if missing or channel_name != expected_name or refusal:
            # A preview that hides this asks somebody to approve a send that
            # cannot happen, and the discovery lands after the approval.
            return (
                SimulationStatus.INCOMPLETE,
                {
                    "missing": missing,
                    "channel_matches": channel_name == expected_name,
                    "credential_refusal": refusal,
                },
            )
        # A preview, and labelled as one. Nothing here asked the channel
        # whether this recipient exists or whether the account may write to
        # that chat; it says what would be sent, exactly. Calling that
        # "complete" invites the reader to hear "verified".
        return (
            SimulationStatus.PREVIEW_ONLY,
            {
                "channel": channel_name,
                "destination": str(payload.get("session_id") or ""),
                "content_bytes": len(str(payload.get("content") or "").encode("utf-8")),
                "network_call_performed": False,
                "checked": ["required fields present", "channel matches this provider"],
                "not_checked": [
                    "whether the recipient exists",
                    "whether this account may write to that destination",
                ],
            },
        )

    async def apply(
        self,
        effect: EffectRecord,
        *,
        idempotency_key: str,
    ) -> ProviderResult:
        if effect.kind != ENTERPRISE_MESSAGE_SEND:
            return ProviderResult(
                ProviderOutcome.KNOWN_NO_EFFECT,
                detail=f"unsupported channel effect kind: {effect.kind}",
            )
        refusal = self._credential_refusal(effect)
        if refusal:
            # Approved earlier, unsendable now: the account this customer's
            # messages belong to is not the one this channel would use. Going
            # ahead would put their content on somebody else's account, which
            # no approval was given for.
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail=refusal)
        if not bool(getattr(self.channel, "is_running", False)):
            return ProviderResult(
                ProviderOutcome.KNOWN_NO_EFFECT,
                detail="channel is not running; no send attempted",
            )

        payload = effect.payload
        channel_name = str(payload.get("channel") or "").strip()
        if channel_name != str(getattr(self.channel, "name", "") or "").strip():
            return ProviderResult(
                ProviderOutcome.KNOWN_NO_EFFECT,
                detail="effect channel does not match provider channel",
            )
        missing = [
            field
            for field in ("user_id", "session_id", "content")
            if not str(payload.get(field) or "").strip()
        ]
        if missing:
            return ProviderResult(
                ProviderOutcome.KNOWN_NO_EFFECT,
                detail="channel effect is missing required fields: "
                + ", ".join(missing),
            )

        message = SystemMessage(
            channel=channel_name,
            user_id=str(payload.get("user_id") or "").strip(),
            session_id=str(payload.get("session_id") or "").strip(),
            content=str(payload.get("content") or ""),
            message_type=str(payload.get("message_type") or "reply"),
            actions=list(payload.get("actions") or []),
            task_ref=str(payload.get("task_ref") or "").strip() or None,
            metadata=dict(payload.get("metadata") or {}),
        )
        await self.channel.send(message)
        receipt = dict(getattr(self.channel, "last_outbound", None) or {})
        receipt.update(
            {
                "provider": self.name,
                "effect_id": effect.effect_id,
                "idempotency_key": idempotency_key,
                # Sending a notification is not the same as the business
                # action being completed.  Keep the lifecycle explicit so
                # callers cannot render an apply receipt as customer
                # feedback or acceptance.
                "delivery_status": "sent",
                "read_status": "unknown",
                "feedback_status": "pending",
                "business_status": "pending_feedback",
            }
        )
        return ProviderResult(ProviderOutcome.APPLIED, receipt=receipt)

    async def verify(
        self, effect: EffectRecord
    ) -> tuple[VerificationStatus, dict[str, Any]]:
        """Verify the durable local receipt without sending another message."""

        receipt = dict(effect.provider_receipt or {})
        if effect.state.value != "applied":
            return VerificationStatus.INCOMPLETE, {"reason": "effect is not applied"}
        if receipt.get("effect_id") != effect.effect_id:
            return VerificationStatus.INCOMPLETE, {
                "reason": "receipt is not bound to effect"
            }
        if receipt.get("idempotency_key") != effect.idempotency_key:
            return VerificationStatus.INCOMPLETE, {
                "reason": "receipt idempotency key mismatch"
            }
        if receipt.get("provider") != self.name:
            return VerificationStatus.INCOMPLETE, {
                "reason": "receipt provider mismatch"
            }
        return VerificationStatus.COMPLETE, {
            "receipt_bound": True,
            "provider": self.name,
            "network_call_performed": False,
            "delivery_status": str(receipt.get("delivery_status") or "sent"),
            "read_status": str(receipt.get("read_status") or "unknown"),
            "feedback_status": str(receipt.get("feedback_status") or "pending"),
            "business_status": str(receipt.get("business_status") or "pending_feedback"),
        }
