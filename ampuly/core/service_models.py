"""Service result types shared by every adapter (console, CLI, board) and the kernel.

They used to live in the office_ui plugin, which made the kernel import a
plugin to raise a business error. Core owns them now; the plugin module
re-exports for compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ServiceEvent:
    """Outbound event to publish on UI transports or consume by CLI callers."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServiceResult:
    """Structured service response plus optional side-effect events."""

    payload: dict[str, Any] = field(default_factory=dict)
    events: list[ServiceEvent] = field(default_factory=list)


class ServiceError(Exception):
    """Expected business error from a shared service."""

    def __init__(self, code: str, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.payload = dict(payload or {})

    def to_payload(self) -> dict[str, Any]:
        # Transport envelope fields are authoritative.  Business details may
        # add context, but must never turn an error acknowledgement into a
        # success or replace the exception's code/message.
        payload = {
            key: value
            for key, value in self.payload.items()
            if key not in {"ok", "error", "code"}
        }
        payload.update({"error": self.message, "code": self.code})
        return payload
