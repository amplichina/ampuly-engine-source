"""Trusted-adapter delivery evidence; no platform validation or network access.

Future real adapters MUST use this conversion for send()/lookup() results and
retain the evidence for audit/reconciliation. They remain responsible for SDK
authentication, account ownership, recipient checks and attributing the platform
receipt to the exact outbound command. Constructing this object does NOT perform
those checks and is not a permission or authentication mechanism. Never build a
trusted receipt from arbitrary raw headers, an LLM claim or a payload trust flag.

Levels are distinct: inbound_ack acknowledges an INCOMING event;
request_accepted acknowledges an outgoing API/SMTP request only;
channel_confirmed is an attributable OUTBOUND platform receipt; delivered and
read require their stronger platform evidence. HTTP 200, SMTP acceptance and a
locally created Message-ID do not establish channel confirmation or delivery.

Rejection requires evidence that this command was NOT sent or queued for later
delivery. A timeout, connection error or generic non-2xx response is insufficient.
adapter_preflight proof is valid only when send was never invoked. If any side
effect may have occurred, report unknown and reconcile rather than blind resend.
"""
from dataclasses import dataclass
from hashlib import sha256

from .contracts import Context, bounded_text

LEVELS = frozenset({"inbound_ack", "request_accepted", "channel_confirmed",
                    "delivered", "read", "unknown", "rejected"})
SOURCES = frozenset({"platform", "http_status", "smtp_acceptance",
                     "local_message_id", "inbound_ack", "adapter_preflight", "unverified"})
CONFIRMING_LEVELS = frozenset({"channel_confirmed", "delivered", "read"})


def _id(value, field):
    bounded_text(value, field, 192)
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError(f"Noncanonical {field}")
    return value


@dataclass(frozen=True)
class DeliveryReceipt:
    """Explicit declaration by a trusted adapter, never a raw response parser.

    context/command_id bind all seven identity fields to the original command.
    source=platform means the adapter has authenticated and checked OUTBOUND
    platform evidence, not merely that an HTTP response came from the platform.
    receipt_id is its attributable platform ID, not a local/generated/request ID.
    not_sent_proof_id identifies retained authoritative rejection evidence or a
    recorded preflight that never invoked send. Identifiers contain no secrets.
    """
    context: Context
    command_id: str
    level: str
    source: str = "unverified"
    receipt_id: str | None = None
    not_sent_proof_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.context, Context):
            raise ValueError("Context required for delivery evidence")
        _id(self.command_id, "command_id")
        if type(self.level) is not str or self.level not in LEVELS:
            raise ValueError("Unsupported evidence level")
        if type(self.source) is not str or self.source not in SOURCES:
            raise ValueError("Unsupported evidence source")
        if self.receipt_id is not None:
            _id(self.receipt_id, "receipt_id")
        if self.not_sent_proof_id is not None:
            _id(self.not_sent_proof_id, "not_sent_proof_id")
            if self.level in CONFIRMING_LEVELS:
                raise ValueError("Confirmation contradicts not-sent evidence")


def to_runtime_result(command, receipt):
    """Return Runtime's small result contract after exact identity matching.

    This checks Context and command_id equality, not a platform signature or
    content hash. Runtime owns immutable command intent and concurrency checks;
    the adapter must verify the recipient/content against that original command
    and must not reuse IDs for modified intent. Evidence-level/source information
    is retained by Runtime separately from the small status result. The adapter
    still retains authoritative platform evidence behind each evidence ID.
    Unknown outcomes have no receipt_id so request IDs cannot appear as success.
    """
    if type(command) is not dict or not isinstance(command.get("context"), Context):
        raise ValueError("Original command with Context required")
    command_id = _id(command.get("command_id"), "original command_id")
    if not isinstance(receipt, DeliveryReceipt):
        raise ValueError("Trusted-adapter DeliveryReceipt required")
    if receipt.context != command["context"] or receipt.command_id != command_id:
        raise ValueError("Receipt does not belong to the exact command context")
    if receipt.source == "platform" and receipt.level in CONFIRMING_LEVELS and receipt.receipt_id is not None:
        return {"status": "confirmed", "receipt_id": receipt.receipt_id}
    if (receipt.level == "rejected" and receipt.source in {"platform", "adapter_preflight"}
            and receipt.not_sent_proof_id is not None):
        return {"status": "rejected"}
    return {"status": "unknown"}


def command_body_digest(command):
    """Digest original outbound text, without interpreting it as instructions."""
    if type(command) is not dict:
        raise ValueError("Original command dictionary required")
    text = bounded_text(command.get("text"), "outbound text", 8000)
    return sha256(text.encode("utf-8")).hexdigest()


def bound_receipt_record(command, receipt):
    """Serializable exact-command evidence for append-only host storage.

    This is evidence attribution, not vendor authentication or permission. The
    original body digest records which stored intent the adapter was asked to
    send; it cannot prove which bytes a dishonest adapter sent to the platform.
    """
    to_runtime_result(command, receipt)
    revision = command.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("Original command revision required")
    return {
        "command_id": _id(command.get("command_id"), "command_id"),
        "context": command["context"].to_dict(),
        "revision": revision,
        "in_reply_to": bounded_text(command.get("in_reply_to"), "in_reply_to"),
        "body_sha256": command_body_digest(command),
        "level": receipt.level,
        "source": receipt.source,
        "receipt_id": receipt.receipt_id,
        "not_sent_proof_id": receipt.not_sent_proof_id,
    }
