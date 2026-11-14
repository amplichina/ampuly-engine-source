"""F-package usability: Preferences safety, connector inventory, IM thin entry, first-use metrics.

Pure contract: no I/O, no Runtime, no ledger writes, no new IM scheduler.
Preferences may sink low-frequency UI prefs; they must not hide safety status.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .ai_employee_contract import AIEmployeeContractError

RULES_VERSION = "f-usability-v1"
SCHEMA_VERSION = 1

_CONNECTOR_STATUSES = (
    "unconfigured",
    "configured_unverified",
    "verified",
    "limited",
    "invalid",
)

_SAFETY_KINDS = (
    "approval_request",
    "permission_gap",
    "connection_fault",
    "failure",
    "unknown",
)

_FIRST_USE_MARKERS = (
    "startup",
    "first_interactive",
    "draft_saved",
    "trial_confirmed",
    "first_event",
    "artifact_visible",
    "reopen_restored",
)


def _err(code: str, message: str, path: str = "", **details: Any) -> AIEmployeeContractError:
    return AIEmployeeContractError(code, message, path, details=details or None)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if type(value) is dict else {}


def assert_preferences_keep_safety_visible(
    *,
    preferences: Mapping[str, Any],
    task_surface_flags: Mapping[str, Any],
) -> dict[str, Any]:
    """F1: quiet/compact preferences must not hide safety on the task surface."""
    prefs = _as_dict(preferences)
    surface = _as_dict(task_surface_flags)
    quiet = bool(prefs.get("quiet_mode") or prefs.get("compact_mode") or prefs.get("hide_reminders"))
    missing: list[str] = []
    for kind in _SAFETY_KINDS:
        key = f"show_{kind}"
        if key not in surface or surface.get(key) is not True:
            missing.append(kind)
    if quiet and missing:
        raise _err(
            "preferences_hide_safety",
            "Preferences 简洁或关闭提醒不得隐藏审批、权限缺口、连接故障、失败与未知。",
            "preferences",
            missing=missing,
        )
    if missing:
        raise _err(
            "preferences_safety_required",
            "任务现场必须保留审批、权限缺口、连接故障、失败与未知状态。",
            "task_surface",
            missing=missing,
        )
    return {
        "ok": True,
        "quiet_mode": quiet,
        "safety_visible": True,
        "rules_version": RULES_VERSION,
        "effects": {"safety_hidden": False},
    }


def build_connector_capability_inventory(
    *,
    connectors: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """F2: disclose connector capabilities and honest verification status."""
    rows_out: list[dict[str, Any]] = []
    for raw in connectors:
        if type(raw) is not dict:
            raise _err("connector_inventory_invalid", "连接器清单项无效。")
        cid = str(raw.get("connector_id") or "").strip()
        if not cid:
            raise _err("connector_id_missing", "连接器需要稳定身份。", "connector_id")
        status = str(raw.get("status") or "").strip()
        if status not in _CONNECTOR_STATUSES:
            raise _err(
                "connector_status_invalid",
                "连接器状态须为未配置/已配置未验证/已验证/受限/失效之一。",
                "status",
                status=status,
            )
        verified_claim = bool(raw.get("claim_verified"))
        if verified_claim and status != "verified":
            raise _err(
                "connector_verified_pretend",
                "未验证连接器不能宣称已验证。",
                "status",
                connector_id=cid,
            )
        disclosure = _as_dict(raw.get("disclosure"))
        required = ("read", "write", "external_side_effects", "auth", "project_scope", "cancel", "readback")
        missing_disc = [k for k in required if k not in disclosure]
        if missing_disc:
            raise _err(
                "connector_disclosure_incomplete",
                "连接器须披露读写、外部副作用、鉴权、项目范围、取消与读回。",
                "disclosure",
                missing=missing_disc,
                connector_id=cid,
            )
        rows_out.append(
            {
                "connector_id": cid,
                "status": status,
                "disclosure": {k: disclosure.get(k) for k in required},
                "pretend_verified": False,
            }
        )
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "rules_version": RULES_VERSION,
        "connectors": rows_out,
        "count": len(rows_out),
        "effects_produced": False,
    }


def plan_im_thin_entry(
    *,
    mode: str,
    identity_bound: bool,
    project_id: str,
    task_id: str | None = None,
    creates_scheduler: bool = False,
) -> dict[str, Any]:
    """F3: IM thin entry is read-only first; reuse identity/project/Task; no new scheduler."""
    if mode not in {"readonly_query", "read"}:
        raise _err(
            "im_entry_mode_invalid",
            "本轮 IM 薄入口仅允许只读查询；写入/通知另验收。",
            "mode",
        )
    if creates_scheduler:
        raise _err(
            "im_scheduler_forbidden",
            "IM 薄入口不能另建调度器。",
            "scheduler",
        )
    if not identity_bound:
        raise _err("im_identity_required", "IM 入口须复用已绑定身份。", "identity")
    if type(project_id) is not str or not project_id.strip():
        raise _err("im_project_required", "IM 入口须复用项目。", "project_id")
    return {
        "ok": True,
        "mode": "readonly_query",
        "project_id": project_id,
        "task_id": task_id,
        "writes": False,
        "scheduler_created": False,
        "rules_version": RULES_VERSION,
        "effects": {"notified": False, "scheduled": False},
    }


def first_use_measurement_report(
    *,
    samples: Iterable[Mapping[str, Any]],
    defer_permission_checks: bool = False,
    hide_unknown: bool = False,
) -> dict[str, Any]:
    """F4: report sample count, median and tail; refuse speed via deferred auth/hidden unknown."""
    if defer_permission_checks:
        raise _err(
            "measurement_defers_permission",
            "不能通过延后权限检查换取速度。",
            "permission",
        )
    if hide_unknown:
        raise _err(
            "measurement_hides_unknown",
            "不能通过隐藏未知状态换取速度。",
            "unknown",
        )
    rows = [dict(s) for s in samples if type(s) is dict]
    if not rows:
        raise _err("measurement_samples_missing", "首次使用测量需要样本。", "samples")
    by_marker: dict[str, list[float]] = {m: [] for m in _FIRST_USE_MARKERS}
    for row in rows:
        marker = str(row.get("marker") or "")
        if marker not in by_marker:
            raise _err("measurement_marker_invalid", "测量标记无效。", "marker", marker=marker)
        try:
            ms = float(row.get("elapsed_ms"))
        except (TypeError, ValueError):
            raise _err("measurement_elapsed_invalid", "耗时须为毫秒数。", "elapsed_ms") from None
        if ms < 0:
            raise _err("measurement_elapsed_invalid", "耗时不能为负。", "elapsed_ms")
        by_marker[marker].append(ms)

    def _median(vals: list[float]) -> float | None:
        if not vals:
            return None
        ordered = sorted(vals)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    def _tail_p95(vals: list[float]) -> float | None:
        if not vals:
            return None
        ordered = sorted(vals)
        idx = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
        return ordered[idx]

    markers_out: dict[str, Any] = {}
    for marker, vals in by_marker.items():
        markers_out[marker] = {
            "sample_count": len(vals),
            "median_ms": _median(vals),
            "tail_p95_ms": _tail_p95(vals),
        }
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "rules_version": RULES_VERSION,
        "sample_count": len(rows),
        "markers": markers_out,
        "permission_deferred": False,
        "unknown_hidden": False,
        "effects_produced": False,
    }

def apply_preferences_safety_gate(
    preferences_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Default-off Preferences safety gate for settings apply/validate paths.

    `None` / empty → skip (legacy settings unchanged). Non-empty must carry
    `preferences` and `task_surface_flags`; then `assert_preferences_keep_safety_visible`
    runs. Never writes settings or hides safety (`safety_hidden` stays False here when skipped).
    """
    if preferences_gate is None or preferences_gate == {}:
        return {
            "ok": True,
            "skipped": True,
            "reason": "gate_off",
            "rules_version": RULES_VERSION,
            "effects": {"safety_hidden": False, "settings_written": False},
        }
    if type(preferences_gate) is not dict:
        raise _err("preferences_gate_invalid", "Preferences 安全门禁参数无效。")
    if "preferences" not in preferences_gate or "task_surface_flags" not in preferences_gate:
        raise _err(
            "preferences_gate_incomplete",
            "启用 Preferences 安全门禁时必须同时提供 preferences 与 task_surface_flags。",
        )
    result = assert_preferences_keep_safety_visible(
        preferences=preferences_gate["preferences"],  # type: ignore[arg-type]
        task_surface_flags=preferences_gate["task_surface_flags"],  # type: ignore[arg-type]
    )
    out = dict(result)
    out["skipped"] = False
    out["reason"] = "gate_enforced"
    out.setdefault("effects", {})["settings_written"] = False
    return out


def preferences_safety_read_api(
    *,
    enabled: bool = False,
    preferences: Mapping[str, Any] | None = None,
    task_surface_flags: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Default-off read-only Preferences safety API for settings surface. No writes."""
    if not enabled:
        return {
            "ok": True,
            "skipped": True,
            "reason": "api_off",
            "rules_version": RULES_VERSION,
            "effects_produced": False,
        }
    result = assert_preferences_keep_safety_visible(
        preferences=preferences or {},
        task_surface_flags=task_surface_flags or {},
    )
    out = dict(result)
    out["skipped"] = False
    out["reason"] = "api_enabled"
    out["effects_produced"] = False
    return out


def connector_inventory_read_api(
    *,
    enabled: bool = False,
    connectors: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Default-off read-only connector capability inventory API. No side effects."""
    if not enabled:
        return {
            "ok": True,
            "skipped": True,
            "reason": "api_off",
            "rules_version": RULES_VERSION,
            "effects_produced": False,
        }
    inv = build_connector_capability_inventory(connectors=connectors)
    out = dict(inv)
    out["skipped"] = False
    out["reason"] = "api_enabled"
    return out


_PENDING_STATUSES = frozenset({
    "pending",
    "awaiting",
    "awaiting_approval",
    "needs_human_review",
    "needs_review",
    "submitted",
    "queued_for_approval",
})

_TERMINAL_APPROVAL_STATUSES = frozenset({
    "done", "completed", "succeeded", "success", "approved", "rejected",
    "denied", "cancelled", "canceled", "expired", "resolved", "closed",
    "failed", "retired", "finished", "superseded",
})


def _approval_revision(row: Mapping[str, Any]) -> int:
    """Only server sequence/revision integers order conflicting duplicate facts."""
    value = row.get("revision", row.get("event_seq", row.get("sequence", 0)))
    return value if type(value) is int and value >= 0 else 0


def _approval_identity(row: Mapping[str, Any]) -> tuple[str, ...] | None:
    rid = str(row.get("id") or row.get("approval_id") or row.get("checkpoint_id") or "").strip()
    if not rid:
        return None
    return tuple(str(row.get(k) or "").strip() for k in
                 ("enterprise_id", "project_id", "customer_id", "environment_id")) + (rid,)


def build_pending_approval_badge(
    *,
    pending_items: Iterable[Mapping[str, Any]] = (),
    preferences: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate pending-approval count for a global badge.

    quiet_mode / compact / hide_reminders must not hide the badge when count > 0
    (same safety posture as Preferences F1). No poll loop; payload is WS-push ready.
    """
    prefs = _as_dict(preferences)
    quiet = bool(prefs.get("quiet_mode") or prefs.get("compact_mode") or prefs.get("hide_reminders"))
    selected: dict[tuple[str, ...], dict[str, Any]] = {}
    unidentified: list[dict[str, Any]] = []
    for raw in pending_items:
        if type(raw) is not dict:
            raise _err("pending_approval_item_invalid", "待审批项无效。")
        row = dict(raw)
        identity = _approval_identity(row)
        if identity is None:
            unidentified.append(row)
            continue
        previous = selected.get(identity)
        if previous is None:
            selected[identity] = row
            continue
        # Equal/absent revisions conservatively prefer terminal evidence over
        # a stale pending duplicate, independent of wire/list ordering.
        status = str(row.get("status") or row.get("state") or "").strip().casefold()
        old_status = str(previous.get("status") or previous.get("state") or "").strip().casefold()
        if (_approval_revision(row), status in _TERMINAL_APPROVAL_STATUSES) > (
            _approval_revision(previous), old_status in _TERMINAL_APPROVAL_STATUSES
        ):
            selected[identity] = row

    rows: list[dict[str, Any]] = []
    unknown_status_items = 0
    for raw in [*selected.values(), *unidentified]:
        status = str(raw.get("status") or raw.get("state") or "").strip().casefold()
        kind = str(raw.get("kind") or raw.get("type") or "").strip().casefold()
        if status in _TERMINAL_APPROVAL_STATUSES:
            continue
        # A present unknown status is not overridden by a type hint or boolean.
        if status and status not in _PENDING_STATUSES:
            unknown_status_items += 1
            continue
        is_pending = status in _PENDING_STATUSES or (
            not status and (kind in {"approval", "approval_request", "blocker"} or raw.get("pending") is True)
        )
        if not is_pending:
            continue
        rid = str(raw.get("id") or raw.get("approval_id") or raw.get("checkpoint_id") or "").strip()
        rows.append({"id": rid, "status": status or kind or "pending"})

    count = len(rows)
    # Explicit preference to hide the badge while quiet is forbidden when items exist.
    if quiet and count > 0 and prefs.get("hide_approval_badge") is True:
        raise _err(
            "preferences_hide_pending_approval_badge",
            "简洁模式不得隐藏待审批全局角标。",
            "preferences",
            count=count,
        )

    visible = count > 0
    label = f"待审批 {count}" if count else ""
    return {
        "ok": True,
        "count": count,
        "count_exact": not unidentified and not unknown_status_items,
        "limited": bool(unidentified or unknown_status_items),
        "unidentified_items": len(unidentified),
        "unknown_status_items": unknown_status_items,
        "visible": visible,
        "quiet_mode": quiet,
        "hidden_by_quiet": False,
        "label": label,
        "items": rows,
        "ws_event": "pending_approval_badge",
        "schema_version": SCHEMA_VERSION,
        "rules_version": RULES_VERSION,
        "effects": {"poll_loop": False, "persisted": False, "safety_hidden": False},
    }


def plan_pending_approval_ws_push(
    *,
    badge: Mapping[str, Any],
    existing_ws_channel: str = "blockers",
    creates_poll_loop: bool = False,
) -> dict[str, Any]:
    """Reuse an existing WS push channel; refuse creating a new poll/execution loop."""
    if creates_poll_loop:
        raise _err(
            "pending_approval_poll_forbidden",
            "待审批角标不得新建轮询或执行循环。",
            "poll",
        )
    channel = str(existing_ws_channel or "").strip()
    if not channel:
        raise _err("pending_approval_ws_channel_required", "须复用已有 WS 推送通道。", "channel")
    body = _as_dict(badge)
    if not body.get("ok"):
        raise _err("pending_approval_badge_invalid", "角标载荷无效。", "badge")
    return {
        "ok": True,
        "channel": channel,
        "event": str(body.get("ws_event") or "pending_approval_badge"),
        "payload": {
            "count": int(body.get("count") or 0),
            "count_exact": bool(body.get("count_exact", False)),
            "limited": bool(body.get("limited", False)),
            "visible": bool(body.get("visible")),
            "label": str(body.get("label") or ""),
            "hidden_by_quiet": False,
        },
        "poll_loop": False,
        "rules_version": RULES_VERSION,
        "effects": {"notified": False, "scheduled": False},
    }


def pending_approval_badge_read_api(
    *,
    enabled: bool = False,
    pending_items: Iterable[Mapping[str, Any]] = (),
    preferences: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Default-off read-only pending-approval badge API. No writes / no poll loop."""
    if not enabled:
        return {
            "ok": True,
            "skipped": True,
            "reason": "api_off",
            "rules_version": RULES_VERSION,
            "effects_produced": False,
        }
    # Read path must never fail closed into "no badge": a stored
    # hide_approval_badge preference (rejected on save by
    # build_pending_approval_badge) is ignored here so quiet users still see
    # pending approvals. Live check 2026-11-01 found the read API raised instead.
    prefs = _as_dict(preferences)
    hide_ignored = prefs.pop("hide_approval_badge", None) is True
    badge = build_pending_approval_badge(
        pending_items=pending_items,
        preferences=prefs,
    )
    out = dict(badge)
    out["hide_preference_ignored"] = hide_ignored
    out["skipped"] = False
    out["reason"] = "api_enabled"
    out["effects_produced"] = False
    return out

