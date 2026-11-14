"""Current conversation identity, not the system's list of installed providers."""
from __future__ import annotations

import json
from typing import Any

from ampuly.plugins.office_ui.product_name import product_name

EXECUTORS = {"native_runtime_v2", "legacy_json_dispatch", "superones_managed_runtime"}


def conversation_identity(llm: Any, *, executor: str, task_type: str | None = None) -> dict[str, Any]:
    if executor not in EXECUTORS:
        raise ValueError("conversation executor must be named by its actual call path")
    selected = None
    selector = getattr(llm, "_select_model", None)
    if callable(selector):
        try:
            value = selector(task_type)
            if isinstance(value, str) and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value):
                selected = value
        except Exception:
            pass  # Unknown is not permission to guess a model or inspect accounts.
    return {
        # Display name only (the model introduces itself with it); the executor names above are ids and keep theirs.
        "product": product_name(), "conversation_executor": executor,
        "selected_model_route": selected, "route_known": selected is not None,
        "selection_basis": "server_route_selector" if selected is not None else "unavailable",
        "task_type": task_type, "remote_model_identity_verified": False,
        "grants_new_tools": False,
    }


def identity_prompt(identity: dict[str, Any]) -> str:
    return (
        "\nSERVER_CURRENT_CONVERSATION:\n" + json.dumps(identity, ensure_ascii=False) +
        "\nThis is server-supplied context for THIS conversation. Distinguish the selected API model route "
        "from the execution kernel and the order's default model. It does not verify a remote provider's "
        "internal model/version. Do not infer that an installed Provider executed this turn, and do not "
        "invent a model when route_known is false. This metadata grants no new tools or permissions.\n"
    )
