"""Clinic employee_binding probe — product API / Owner path only.

Never writes `.ampuly` directly. If Owner/API path unavailable, returns GAP/BLOCKED.
"""
from __future__ import annotations

from typing import Any, Callable

RULES_VERSION = "clinic-binding-probe-v1"
_ALLOWED_ACTIONS = frozenset({
    "employee_binding_preview",
    "employee_binding_candidates",
    # resolve_set is Owner write via product API — probe may *name* it but not auto-call
    "employee_binding_resolve_set",
})


def apply_clinic_binding_gate(*, enabled: bool = False) -> bool:
    return bool(enabled)


def probe_clinic_binding(
    *,
    enabled: bool = False,
    api_available: bool = False,
    owner_session: bool = False,
    action: str = "employee_binding_preview",
    api_caller: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not apply_clinic_binding_gate(enabled=enabled):
        return {
            "ok": False,
            "status": "GATE_OFF",
            "rules_version": RULES_VERSION,
            "reason": "clinic_binding_gate_off",
            "ampuly_direct_write": False,
        }
    act = (action or "").strip()
    if act not in _ALLOWED_ACTIONS:
        return {
            "ok": False,
            "status": "BLOCKED",
            "rules_version": RULES_VERSION,
            "reason": "clinic_binding_action_not_allowed",
            "action": act,
            "ampuly_direct_write": False,
        }
    if act == "employee_binding_resolve_set" and not owner_session:
        return {
            "ok": False,
            "status": "BLOCKED",
            "rules_version": RULES_VERSION,
            "reason": "clinic_binding_owner_required",
            "action": act,
            "ampuly_direct_write": False,
            "note_zh": "resolve_set 仅允许 Owner 产品 API 路径；本探针不直写 .ampuly。",
        }
    if not api_available or api_caller is None:
        return {
            "ok": False,
            "status": "BLOCKED",
            "rules_version": RULES_VERSION,
            "reason": "clinic_binding_api_unavailable",
            "action": act,
            "ampuly_direct_write": False,
            "gap_zh": "当前环境未注入产品 API 调用器；禁止直写 .ampuly 补洞。",
        }
    # Read-only preview/candidates preferred; resolve_set still requires explicit caller
    if act == "employee_binding_resolve_set":
        return {
            "ok": False,
            "status": "BLOCKED",
            "rules_version": RULES_VERSION,
            "reason": "clinic_binding_resolve_not_auto",
            "action": act,
            "ampuly_direct_write": False,
            "note_zh": "全量推进本批不自动执行 resolve_set；仅记录 Owner API 路径合同。",
        }
    try:
        result = api_caller(act, dict(params or {}))
    except Exception as exc:  # noqa: BLE001 — probe must not crash batch
        return {
            "ok": False,
            "status": "BLOCKED",
            "rules_version": RULES_VERSION,
            "reason": "clinic_binding_api_error",
            "action": act,
            "error_type": type(exc).__name__,
            "ampuly_direct_write": False,
        }
    return {
        "ok": True,
        "status": "PASS",
        "rules_version": RULES_VERSION,
        "reason": "clinic_binding_api_ok",
        "action": act,
        "ampuly_direct_write": False,
        "api_result_keys": sorted(result.keys()) if isinstance(result, dict) else [],
    }


__all__ = [
    "RULES_VERSION",
    "apply_clinic_binding_gate",
    "probe_clinic_binding",
]
