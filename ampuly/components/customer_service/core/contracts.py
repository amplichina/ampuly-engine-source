"""Proposed lab contracts; these do not describe an existing Engine 4.7 API."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .delivery import DeliveryReceipt

SCHEMA_VERSION = "ampuly.cs.lab/0.1"
MAX_INBOUND_TEXT_CHARS = 2000


def bounded_text(value, field, maximum=192):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"Invalid {field}")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError(f"Invalid {field}")
    return value


@dataclass(frozen=True)
class Context:
    tenant_id: str
    shop_id: str
    seller_id: str
    buyer_id: str
    conversation_id: str
    channel: str = "qianniu"
    account_id: str = "lab-account"

    def __post_init__(self):
        for name, value in asdict(self).items():
            bounded_text(value, name, 128)
            if value != value.strip() or "\n" in value or "\t" in value:
                raise ValueError(f"Noncanonical {name}")

    def to_dict(self):
        return asdict(self)

    @property
    def key(self):
        return sha256(canonical_json(self.to_dict())).hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


class ChannelAdapter(Protocol):
    """Trusted adapter boundary, not an LLM tool or arbitrary-code interface.

    A real implementation must validate recipient/ownership immediately at the
    side-effect boundary, preserve command_id, and support authoritative lookup.
    A snapshot alone cannot eliminate a takeover race inside another process.
    Return bound DeliveryReceipt evidence; Runtime itself validates identity and
    delivery level. None means uncertain. Bare result dictionaries cannot confirm
    or reject a send. This protocol performs no platform authentication itself.
    """

    def snapshot(self, context: Context) -> dict: ...
    def send(self, command: dict) -> DeliveryReceipt | None: ...
    def lookup(self, command: dict) -> DeliveryReceipt | None: ...
