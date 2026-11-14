"""Q5 kickoff: admit an external-agent / ecommerce *goal* without lying.

This is the inbound half of "controlled ecommerce + external Agent" — not the
outbound ExternalAgentBroker spawn path, and not canvas generation. A caller
hands in a signed envelope. We check, in order:

1. Shop / Alibaba-style channels that are still *pending_protocol* refuse
   honestly (reuse inbound_entry.adapter_status) — never report success.
2. HMAC-SHA256 signature over a canonical payload (local shared secret; tests
   pass the secret explicitly; production may set AMPULY_EXTERNAL_AGENT_SECRET).
3. Project allowlist — unknown project_id is unauthorized.
4. Dedup via InboundDedup — providers redeliver.
5. Monotonic sequence per project — out-of-order / replay with new id fails.

On accept we return a skeleton admission only: no LLM, no paid API, no shop
write. The materials field is hashed, never executed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from typing import Any

from ampuly.channels.inbound_dedup import InboundDedup
from ampuly.channels.inbound_entry import InboundNotSupported, adapter_status
from ampuly.project_id import is_valid_project_id

CHANNEL_NAME = "external_agent"
DEFAULT_ENV_SECRET = "AMPULY_EXTERNAL_AGENT_SECRET"


class IngressRejected(RuntimeError):
    """Envelope failed a gate; reason is stable for tests and logs."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = str(reason or "rejected")
        self.detail = str(detail or "")
        super().__init__(f"{self.reason}: {self.detail}" if self.detail else self.reason)


@dataclass
class ExternalAgentEnvelope:
    message_id: str
    project_id: str
    sequence: int
    goal: str
    materials_digest: str = ""
    signature: str = ""
    channel: str = CHANNEL_NAME

    def canonical_payload(self) -> bytes:
        body = {
            "message_id": str(self.message_id or ""),
            "project_id": str(self.project_id or ""),
            "sequence": int(self.sequence),
            "goal": str(self.goal or ""),
            "materials_digest": str(self.materials_digest or ""),
            "channel": str(self.channel or CHANNEL_NAME).strip().lower() or CHANNEL_NAME,
        }
        return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass
class ExternalAgentAdmission:
    accepted: bool
    status: str
    message_id: str
    project_id: str
    sequence: int
    goal_preview: str = ""
    materials_digest: str = ""
    reason: str = ""
    screen: dict[str, Any] = field(default_factory=dict)
    # Preserve the signed input separately from its display-only preview.
    goal: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "status": self.status,
            "message_id": self.message_id,
            "project_id": self.project_id,
            "sequence": self.sequence,
            "goal_preview": self.goal_preview,
            "goal": self.goal,
            "materials_digest": self.materials_digest,
            "reason": self.reason,
            "screen": dict(self.screen),
        }


def materials_digest_of(raw: str | bytes) -> str:
    data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw or b"")
    return hashlib.sha256(data).hexdigest()


def sign_envelope(envelope: ExternalAgentEnvelope, secret: str | bytes) -> str:
    key = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret or b"")
    return hmac.new(key, envelope.canonical_payload(), hashlib.sha256).hexdigest()


def resolve_secret(explicit: str | bytes | None = None) -> bytes:
    if explicit is not None:
        if not isinstance(explicit, (str, bytes)):
            raise IngressRejected("invalid_secret")
        key = explicit.encode("utf-8") if isinstance(explicit, str) else explicit
        if not key:
            raise IngressRejected("missing_secret")
        return key
    env = os.environ.get(DEFAULT_ENV_SECRET, "")
    if not env:
        raise IngressRejected("missing_secret", f"{DEFAULT_ENV_SECRET} not set and no explicit secret")
    return env.encode("utf-8")


def _preview(goal: str, limit: int = 120) -> str:
    text = " ".join(str(goal or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class ExternalAgentIngress:
    """Stateful admitter: dedup + last sequence per project."""

    def __init__(
        self,
        *,
        secret: str | bytes | None = None,
        allowed_projects: frozenset[str] | set[str] | tuple[str, ...] | None = None,
        dedup: InboundDedup | None = None,
        dedup_path: Any = None,
    ) -> None:
        self._secret = resolve_secret(secret)
        if allowed_projects is not None and not isinstance(allowed_projects, (set, frozenset, tuple)):
            raise IngressRejected("invalid_project_allowlist")
        if any(not is_valid_project_id(p) for p in (allowed_projects or ())):
            raise IngressRejected("invalid_project_allowlist")
        # No grant means no project. Never interpret an empty allowlist as '*'.
        self._allowed = frozenset(allowed_projects or ())
        self._dedup = dedup or InboundDedup(CHANNEL_NAME, path=dedup_path)
        self._last_seq: dict[str, int] = {}

    def admit(self, envelope: ExternalAgentEnvelope) -> ExternalAgentAdmission:
        admission = self.verify(envelope)
        last = self._last_seq.get(admission.project_id, 0)
        if admission.sequence <= last:
            raise IngressRejected("out_of_order", f"seq={admission.sequence} last={last}")
        if not self._dedup.remember(admission.message_id):
            raise IngressRejected("duplicate", admission.message_id)
        self._last_seq[admission.project_id] = admission.sequence
        return admission

    def verify(self, envelope: ExternalAgentEnvelope) -> ExternalAgentAdmission:
        """Verify without consuming a request; formal Task creation owns dedup."""
        channel = str(envelope.channel or CHANNEL_NAME).strip().lower() or CHANNEL_NAME
        # Shop pending honesty first — never claim delivery for unfinished protocols.
        if channel != CHANNEL_NAME:
            status = adapter_status(channel)
            if not status.get("available"):
                raise InboundNotSupported(channel, tuple(status.get("still_needed") or ()))
            raise IngressRejected("channel_not_external_agent", channel)

        project_id = envelope.project_id
        if not project_id:
            raise IngressRejected("missing_project")
        if not is_valid_project_id(project_id):
            raise IngressRejected("invalid_project")
        if project_id not in self._allowed:
            raise IngressRejected("unauthorized_project", project_id)

        message_id = str(envelope.message_id or "").strip()
        if not message_id:
            raise IngressRejected("missing_message_id")

        # Validate before canonical_payload coerces to int: bools, fractions
        # and numeric strings must not share the signed integer identity.
        sequence = envelope.sequence
        if type(sequence) is not int or sequence < 1 or sequence > 2**63 - 1:
            raise IngressRejected("bad_sequence")
        if not isinstance(envelope.goal, str) or not envelope.goal.strip():
            raise IngressRejected("missing_goal")

        expected = hmac.new(self._secret, envelope.canonical_payload(), hashlib.sha256).hexdigest()
        offered = str(envelope.signature or "").strip().lower()
        if not offered or not hmac.compare_digest(expected, offered):
            raise IngressRejected("bad_signature")

        digest = str(envelope.materials_digest or "").strip()
        return ExternalAgentAdmission(
            accepted=True,
            status="skeleton_accepted",
            message_id=message_id,
            project_id=project_id,
            sequence=sequence,
            goal_preview=_preview(envelope.goal),
            goal=str(envelope.goal or ""),
            materials_digest=digest,
            reason="q5_kickoff_skeleton",
            screen={"channel": CHANNEL_NAME, "paid_api": False, "canvas_draft": False},
        )


def admit_external_agent_goal(
    envelope: ExternalAgentEnvelope,
    *,
    secret: str | bytes | None = None,
    allowed_projects: frozenset[str] | set[str] | tuple[str, ...] | None = None,
    dedup: InboundDedup | None = None,
    dedup_path: Any = None,
) -> ExternalAgentAdmission:
    """One-shot helper; prefer ExternalAgentIngress when sequencing many goals."""
    return ExternalAgentIngress(
        secret=secret,
        allowed_projects=allowed_projects,
        dedup=dedup,
        dedup_path=dedup_path,
    ).admit(envelope)


def signed_envelope(
    *,
    message_id: str,
    project_id: str,
    sequence: int,
    goal: str,
    secret: str | bytes,
    materials: str | bytes = b"",
    channel: str = CHANNEL_NAME,
) -> ExternalAgentEnvelope:
    """Test/helper builder: digest materials and attach HMAC."""
    digest = materials_digest_of(materials) if materials else ""
    env = ExternalAgentEnvelope(
        message_id=message_id,
        project_id=project_id,
        sequence=sequence,
        goal=goal,
        materials_digest=digest,
        channel=channel,
    )
    env.signature = sign_envelope(env, secret)
    return env
