"""Name what an inbound message is trying to do, without believing it.

Everything arriving from a channel is already marked ``untrusted_external``
at the boundary, and that marking clamps how much a turn may do. But "this
came from outside" is one bit; two messages that both carry it can be very
different things: a customer describing a logo, and a message telling the
assistant to ignore its instructions and send an API key.

This screens inbound text and records what it *looks like* -- instruction
override, a request for approval, a request for credentials, a link, code.
It changes nothing: the content is passed through untouched, no message is
blocked here, and no flag grants anything. The flags travel in metadata so
the runtime and the person reading the approval card can see the shape of
what arrived.

Deliberately not a classifier with a model: a regex list is auditable, runs
offline, and cannot itself be talked out of its answer by the text it reads.
"""

from __future__ import annotations

import re
from typing import Any

# Attempts to redirect the assistant's instructions.
_INSTRUCTION_PATTERNS = (
    r"ignore (all |any |the )?(previous|prior|above|earlier)",
    r"disregard (all |any |the )?(previous|prior|above|earlier)",
    r"forget (your|the|all) (instructions|rules|prompt)",
    r"(system|developer) (prompt|message|mode)",
    r"you are now\b",
    r"from now on,? (you|act)",
    r"忽略(之前|上面|前面|所有)",
    r"无视(之前|上面|前面)",
    r"忘记(你的|之前的)(指令|规则|设定)",
    r"从现在开始[，,]?\s*你",
    r"开发者模式",
    r"扮演(一个)?",
)

# Asking for an action a person is supposed to authorise.
_APPROVAL_PATTERNS = (
    r"\b(approve|authorize|authorise|confirm) (this|it|the)",
    r"\bjust (run|execute|do) it\b",
    r"no need to ask",
    r"(直接|不用问|无需)(批准|确认|执行)",
    r"(帮我|请)(批准|放行|通过)",
)

# Asking for something that must never leave the machine.
_CREDENTIAL_PATTERNS = (
    r"\bapi[_ -]?key\b",
    r"\b(access|refresh|auth)[_ -]?token\b",
    r"\bpassword\b",
    r"\b(private|secret) key\b",
    r"(密钥|口令|密码|凭据|访问令牌)",
)

_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_CODE_FENCE = re.compile(r"```|\bcurl\b|\bpowershell\b|\bInvoke-WebRequest\b", re.IGNORECASE)
# Long messages are where an override tends to hide after a wall of context.
LONG_MESSAGE_CHARS = 4000


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def screen_inbound(content: Any) -> list[str]:
    """Flags describing what an inbound message looks like; never a verdict.

    An empty list means nothing matched, which is not the same as safe.
    """
    text = str(content or "")
    if not text.strip():
        return []
    flags: list[str] = []
    if _matches(_INSTRUCTION_PATTERNS, text):
        flags.append("instruction_override")
    if _matches(_APPROVAL_PATTERNS, text):
        flags.append("approval_request")
    if _matches(_CREDENTIAL_PATTERNS, text):
        flags.append("credential_request")
    if _URL.search(text):
        flags.append("contains_url")
    if _CODE_FENCE.search(text):
        flags.append("contains_code")
    if len(text) >= LONG_MESSAGE_CHARS:
        flags.append("long_message")
    return flags


def stamp_screen(metadata: dict[str, Any], content: Any) -> dict[str, Any]:
    """Record the screening on a message's metadata, in place.

    The flags are evidence for whoever reads the card, not permission for
    anything: no flag widens what a turn may do, and their absence does not
    make content trusted -- the boundary marker already said what it is.
    """
    flags = screen_inbound(content)
    metadata["content_screen"] = flags
    metadata["content_screen_version"] = 1
    return metadata
