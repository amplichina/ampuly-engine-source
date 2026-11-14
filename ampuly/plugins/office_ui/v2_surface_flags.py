"""V2 C/D/E/F surface flags — WS/frontend entries default off.

Contract only: does not open WebSocket handlers or mutate settings.
"""
from __future__ import annotations

from typing import Any

RULES_VERSION = "v2-surface-flags-v1"

# All default False. Enabling a flag only means "UI/WS entry may be shown";
# underlying gates (composition/publish/17E/usability) remain separate and default-off.
DEFAULT_FLAGS: dict[str, bool] = {
    "c_ws_preview": False,
    "c_ws_approve": False,
    "c_ws_propose": False,
    "c_frontend_entry": False,
    "d_ws_composition_gate": False,
    "d_frontend_publish_entry": False,
    "e_ws_experience_gate": False,
    "e_frontend_task_center_entry": False,
    "e_ws_task_center_read": False,
    "f_ws_usability_read": False,
    "f_frontend_preferences_safety": False,
    "f_frontend_connector_inventory": False,
    "f_frontend_pending_approval_badge": False,
    "f_ws_pending_approval_badge": False,
}


def apply_v2_surface_flags_gate(*, enabled: bool = False) -> bool:
    return bool(enabled)


def list_v2_surface_flags(*, enabled: bool = False) -> dict[str, Any]:
    if not apply_v2_surface_flags_gate(enabled=enabled):
        return {
            "ok": False,
            "enabled": False,
            "rules_version": RULES_VERSION,
            "reason": "v2_surface_flags_gate_off",
            "flags": None,
        }
    return {
        "ok": True,
        "enabled": True,
        "rules_version": RULES_VERSION,
        "reason": "flags_readonly",
        "flags": dict(DEFAULT_FLAGS),
        "any_surface_on": any(DEFAULT_FLAGS.values()),
        "note_zh": "全部默认关；打开旗标不自动放开后端门禁或写入。",
    }


def is_surface_enabled(flag: str, *, enabled: bool = False) -> bool:
    """Return whether a named surface may render. Gate-off => always False."""
    if not apply_v2_surface_flags_gate(enabled=enabled):
        return False
    return bool(DEFAULT_FLAGS.get(flag, False))


__all__ = [
    "RULES_VERSION",
    "DEFAULT_FLAGS",
    "apply_v2_surface_flags_gate",
    "list_v2_surface_flags",
    "is_surface_enabled",
]
