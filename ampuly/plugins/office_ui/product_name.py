"""The product's name as the owner sees it.

Display only: the duty officer's messages, a DingTalk message title, the
master control's self-introduction and the chat speaker of the engine's own
voice (``display_speaker``) say this name.  Protocol names (``ampuly``),
database identities, environment variable names, module names and ids keep
their old spelling -- a rebrand does not rename the protocol or the database
identity (CLAUDE.md).  The console's own title lives in the frontend.

Plugin-layer module: no engine-kernel changes.
"""

from __future__ import annotations

from typing import Any

from ampuly.i18n_runtime import is_zh

PRODUCT_NAME_ZH = "安普里"
PRODUCT_NAME_EN = "Ampuly"

# The name the engine's own voice is stored under -- in the chat cache's sender_name column, and in the kernel
# runtime's ``visible_speaker`` metadata. A stored identity, not a display name: rows keep it and nothing migrates
# them; what the owner reads is ``display_speaker`` of it, said when the row is read or sent.
STORED_SPEAKER = "SuperOnes"


def product_name(zh: bool | None = None) -> str:
    """The name in the owner's language (the response language unless ``zh`` says otherwise)."""
    if zh is None:
        zh = is_zh()
    return PRODUCT_NAME_ZH if zh else PRODUCT_NAME_EN


def display_speaker(name: Any) -> Any:
    """The speaker name the owner sees for a stored or engine-given one.

    The engine's own voice (``STORED_SPEAKER``) is shown as the product's name in the owner's language at the moment
    it is shown; every other name -- a role, a worker, 总控, You -- is shown as it is, and so is a value that is not
    a string. Callers that map the internal ``task_generalist`` label to the engine's voice do so first (they know
    the message kind) and pass ``STORED_SPEAKER`` here.
    """
    if isinstance(name, str) and name.strip().casefold() == STORED_SPEAKER.casefold():
        return product_name()
    return name
