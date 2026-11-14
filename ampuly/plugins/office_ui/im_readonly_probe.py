"""IM real readonly probe — query planning only; never writes.

If channel credentials are absent, returns BLOCKED with evidence-friendly payload.
Does not print secrets.
"""
from __future__ import annotations

from typing import Any, Mapping

from .employee_usability import plan_im_thin_entry

RULES_VERSION = "im-readonly-probe-v1"
_SUPPORTED = ("feishu", "dingtalk", "wecom")


def apply_im_readonly_gate(*, enabled: bool = False) -> bool:
    return bool(enabled)


def probe_im_readonly(
    *,
    channel: str,
    identity_bound: bool,
    project_id: str,
    channel_configured: bool | None = None,
    credential_present: bool | None = None,
    task_id: str | None = None,
    enabled: bool = False,
) -> dict[str, Any]:
    """Plan a readonly IM query. Writes always false. Missing creds => BLOCKED."""
    if not apply_im_readonly_gate(enabled=enabled):
        return {
            "ok": False,
            "status": "GATE_OFF",
            "rules_version": RULES_VERSION,
            "reason": "im_readonly_gate_off",
            "writes": False,
        }
    ch = (channel or "").strip().lower()
    if ch not in _SUPPORTED:
        return {
            "ok": False,
            "status": "BLOCKED",
            "rules_version": RULES_VERSION,
            "reason": "im_channel_unsupported",
            "channel": ch,
            "writes": False,
        }
    try:
        plan = plan_im_thin_entry(
            mode="readonly_query",
            identity_bound=identity_bound,
            project_id=project_id,
            task_id=task_id,
            creates_scheduler=False,
        )
    except Exception as exc:  # noqa: BLE001 — probe returns BLOCKED, never writes
        return {
            "ok": False,
            "status": "BLOCKED",
            "rules_version": RULES_VERSION,
            "reason": "im_plan_rejected",
            "error_type": type(exc).__name__,
            "writes": False,
        }
    configured = bool(channel_configured) if channel_configured is not None else False
    creds = bool(credential_present) if credential_present is not None else False
    if not configured or not creds:
        return {
            "ok": False,
            "status": "BLOCKED",
            "rules_version": RULES_VERSION,
            "reason": "im_credentials_missing",
            "channel": ch,
            "channel_configured": configured,
            "credential_present": creds,
            "writes": False,
            "plan": plan,
            "note_zh": "无渠道配置或凭证；只记录 BLOCKED，不读取密钥内容、不写入。",
        }
    return {
        "ok": True,
        "status": "PASS_PLANNED",
        "rules_version": RULES_VERSION,
        "reason": "im_readonly_planned",
        "channel": ch,
        "channel_configured": True,
        "credential_present": True,
        "writes": False,
        "plan": plan,
        "note_zh": "仅规划只读查询；本探针不发起真实 IM 网络调用（避免无授权外发）。",
    }


def summarize_credential_flags(channel_status: Mapping[str, Any] | None) -> dict[str, bool]:
    """Derive non-secret flags from a status mapping (never values)."""
    st = dict(channel_status or {})
    return {
        "channel_configured": bool(st.get("configured") or st.get("channel_configured")),
        "credential_present": bool(st.get("credential_present") or st.get("has_credential") or st.get("token_set")),
    }


__all__ = [
    "RULES_VERSION",
    "apply_im_readonly_gate",
    "probe_im_readonly",
    "summarize_credential_flags",
]
