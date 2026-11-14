"""E-package task center: read-only task facts and experience revision gates.

Pure contract: no I/O, no Runtime, no ledger writes, no scheduler, no auto-adopt.
Extends B read-only timeline facts into a unified task-center projection.
Experience stays candidate until an explicit adopt path elsewhere; this module
only gates promotion on shared regression samples.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

from .ai_employee_contract import AIEmployeeContractError, canonical_json
from .employee_verifiable_template import knowledge_revoke_impact

RULES_VERSION = "e-task-center-v1"
SCHEMA_VERSION = 1

_CANDIDATE_WORDING = "新增候选修订"
_FORBIDDEN_WORDING = ("已学会", "准确率提升", "已自动学会", "accuracy improved", "learned")


def _err(code: str, message: str, path: str = "", **details: Any) -> AIEmployeeContractError:
    return AIEmployeeContractError(code, message, path, details=details or None)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if type(value) is dict else {}


def feedback_record_wording(kind: str = "candidate") -> dict[str, Any]:
    """Saving feedback may only claim a new candidate revision."""
    if kind not in {"candidate", "candidate_revision"}:
        raise _err("experience_wording_invalid", "反馈只能记为新增候选修订。", "kind")
    return {
        "ok": True,
        "wording": _CANDIDATE_WORDING,
        "forbidden": list(_FORBIDDEN_WORDING),
        "rules_version": RULES_VERSION,
        "effects": {"adopted_changed": False, "publish_performed": False},
    }


def assert_feedback_wording(text: str) -> dict[str, Any]:
    if type(text) is not str or not text.strip():
        raise _err("experience_wording_invalid", "反馈文案无效。")
    lowered = text.casefold()
    for bad in _FORBIDDEN_WORDING:
        if bad.casefold() in lowered or bad in text:
            raise _err(
                "experience_wording_forbidden",
                "只保存反馈时写「新增候选修订」，不能写已学会或准确率提升。",
                "wording",
            )
    if _CANDIDATE_WORDING not in text:
        raise _err(
            "experience_wording_required",
            "反馈记录必须使用「新增候选修订」。",
            "wording",
        )
    return {"ok": True, "wording": _CANDIDATE_WORDING, "rules_version": RULES_VERSION}


def assert_experience_promotion_allowed(
    *,
    candidate: Mapping[str, Any],
    adopted_version_id: str | None,
    regression_results: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Black-box 17E: shared regression sample regresses → refuse promotion."""
    if type(candidate) is not dict or not str(candidate.get("version_id") or "").strip():
        raise _err("experience_candidate_invalid", "晋升需要有效的经验候选。", "candidate")
    status = str(candidate.get("status") or "candidate")
    if status not in {"candidate", "needs_human_review"}:
        raise _err("experience_not_a_candidate", "只有候选版本可申请晋升。", "status")
    rows = list(regression_results or [])
    if not rows:
        raise _err(
            "experience_regression_missing",
            "晋升需要共同回归样本结果，不能在无回归证据时放行。",
            "regression_results",
        )
    regressions: list[str] = []
    shared_seen = False
    for row in rows:
        if type(row) is not dict:
            raise _err("experience_regression_invalid", "回归结果结构无效。")
        shared = bool(row.get("shared"))
        if shared:
            shared_seen = True
        sample_id = str(row.get("sample_id") or "")
        baseline_ok = bool(row.get("baseline_ok"))
        candidate_ok = bool(row.get("candidate_ok"))
        if shared and baseline_ok and not candidate_ok:
            regressions.append(sample_id or "unknown")
    if not shared_seen:
        raise _err(
            "experience_regression_missing",
            "晋升需要至少一个共同回归样本。",
            "regression_results",
        )
    if regressions:
        raise _err(
            "experience_promotion_regressed",
            "反馈候选回归出现退步，阻止晋升；当前有效知识及 SOP 不改变。",
            "regression",
            sample_ids=regressions,
            adopted_version_id=adopted_version_id,
        )
    return {
        "ok": True,
        "promote_allowed": True,
        "candidate_version_id": candidate.get("version_id"),
        "adopted_version_id": adopted_version_id,
        "rules_version": RULES_VERSION,
        "effects": {"adopted_changed": False, "publish_performed": False},
    }


def assert_replay_readonly(*, mode: str) -> dict[str, Any]:
    if mode not in {"replay", "lookback", "review"}:
        raise _err("replay_mode_invalid", "回看模式无效。", "mode")
    return {
        "ok": True,
        "mode": mode,
        "rerun": False,
        "rules_version": RULES_VERSION,
        "effects": {"executed": False},
    }


def require_explicit_rerun_request(
    *,
    original_task_id: str,
    new_request_id: str,
    diff_ref: str,
) -> dict[str, Any]:
    if type(original_task_id) is not str or not original_task_id.strip():
        raise _err("rerun_original_missing", "再次试跑必须关联原任务。", "original_task_id")
    if type(new_request_id) is not str or not new_request_id.strip():
        raise _err("rerun_request_missing", "再次试跑必须显式创建新请求。", "new_request_id")
    if new_request_id == original_task_id:
        raise _err("rerun_request_not_new", "再次试跑不能复用原任务身份。", "new_request_id")
    if type(diff_ref) is not str or not diff_ref.strip():
        raise _err("rerun_diff_missing", "再次试跑必须关联新旧差异。", "diff_ref")
    return {
        "ok": True,
        "original_task_id": original_task_id,
        "new_request_id": new_request_id,
        "diff_ref": diff_ref,
        "rules_version": RULES_VERSION,
        "effects": {"executed": False},
    }


def controlled_restore_plan(
    *,
    target_version_ref: str,
    revoked_refs: Iterable[str] = (),
    revoked_capabilities: Iterable[str] = (),
) -> dict[str, Any]:
    if type(target_version_ref) is not str or not target_version_ref.strip():
        raise _err("restore_ref_invalid", "恢复目标版本引用无效。", "target_version_ref")
    revoked = [str(x) for x in revoked_refs if str(x).strip()]
    caps = [str(x) for x in revoked_capabilities if str(x).strip()]
    return {
        "ok": True,
        "restore_version_ref": target_version_ref,
        "allowed": True,
        "blocked_revoked_refs": revoked,
        "blocked_revoked_capabilities": caps,
        "restores_revoked_access": False,
        "rules_version": RULES_VERSION,
        "effects": {"restored": False},
    }


def project_legacy_config_readonly(raw: Mapping[str, Any]) -> dict[str, Any]:
    if type(raw) is not dict:
        raise _err("legacy_config_invalid", "旧配置投影需要对象。")
    known = ("role", "knowledge", "sop", "output", "permissions", "name", "version")
    mapped: dict[str, Any] = {}
    unknown: dict[str, Any] = {}
    for key, value in raw.items():
        if key in known:
            mapped[key] = value
        else:
            unknown[key] = value
    return {
        "ok": True,
        "mapped": mapped,
        "unknown_fields": unknown,
        "migrated": False,
        "editable": False,
        "rules_version": RULES_VERSION,
        "effects": {"written": False},
    }


def compare_history_snapshots(
    older: Iterable[Mapping[str, Any]],
    newer: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare two fact lists by id; newer determined facts win; no writes."""
    def index(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            if type(row) is not dict:
                continue
            key = str(row.get("id") or row.get("event_id") or "")
            if not key:
                continue
            out[key] = dict(row)
        return out

    a = index(older)
    b = index(newer)
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    changed = sorted(
        key for key in set(a) & set(b)
        if canonical_json(a[key]) != canonical_json(b[key])
    )
    return {
        "ok": True,
        "added": added,
        "removed": removed,
        "changed": changed,
        "rules_version": RULES_VERSION,
        "effects": {"written": False},
    }


def explain_composition(
    composition_record: Mapping[str, Any] | None,
    approval: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if composition_record is None:
        return {
            "summary": None,
            "approval_bound": False,
            "composition_id": None,
            "limited": True,
            "reason": "composition_absent",
        }
    if type(composition_record) is not dict:
        raise _err("composition_invalid", "组合记录无效。")
    cid = composition_record.get("composition_id")
    bound = False
    if approval is not None:
        if type(approval) is not dict:
            raise _err("composition_approval_invalid", "批准记录无效。")
        bound = (
            approval.get("composition_id") == cid
            and approval.get("effective_config_digest")
            == composition_record.get("effective_config_digest")
        )
    effective = _as_dict(composition_record.get("effective"))
    return {
        "summary": {
            "composition_id": cid,
            "project_id": composition_record.get("project_id"),
            "purpose": composition_record.get("purpose"),
            "capabilities": effective.get("capabilities") or {},
            "knowledge_refs": effective.get("knowledge_refs") or [],
            "capability_gaps": effective.get("capability_gaps") or [],
        },
        "approval_bound": bound,
        "composition_id": cid,
        "limited": False,
        "reason": None,
    }



_LIVE_STATUSES = frozenset({"running", "in_progress", "active", "live", "working", "executing"})
_INTERRUPTED_STATUSES = frozenset({
    "interrupted", "blocked", "suspended", "failed", "error", "cancelled", "canceled", "stalled", "paused",
    "outcome_unknown", "effect_unknown", "unknown", "approval_required", "awaiting_approval",
    "awaiting_human", "waiting_approval", "cancel_requested", "stop_requested", "stopping",
})
_IDLE_STATUSES = frozenset({"idle", "completed", "succeeded", "done", "finished", "closed", "ok"})
_TERMINAL_SUCCESS_STATUSES = _IDLE_STATUSES - {"idle"}
_PRE_LEASE_STATUSES = frozenset({"", "pending", "queued", "dispatched", "starting", "waiting_dispatch"})

# Display-only grace after server dispatch before first runtime stamp.
# Does NOT grant execution eligibility (see grants_execution / display_only).
PRE_LEASE_DISPLAY_GRACE_MS = 10_000


def _coerce_ms(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        n = float(text)
        return n if math.isfinite(n) and n >= 0 else None
    except ValueError:
        return None


def _generation_of(facts: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        raw = facts.get(key)
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (str, int)):
            continue
        text = str(raw).strip()
        if isinstance(raw, int) and raw < 0:
            continue
        if text and len(text) <= 256 and not any(ch.isspace() or ord(ch) < 32 for ch in text):
            return text
    return ""


def _apply_pre_lease_display_grace(
    *,
    health: str,
    status: str,
    facts: Mapping[str, Any],
    now_ms: float | None,
) -> tuple[str, dict[str, Any]]:
    """If just dispatched and unstamped, keep UI live briefly without execution rights."""
    meta: dict[str, Any] = {
        "pre_lease_considered": True,
        "display_only": True,
        "grants_execution": False,
        "grace_ms": PRE_LEASE_DISPLAY_GRACE_MS,
        "applied": False,
        "reason": "",
    }
    # Never override interrupted / approval / cancel governance with a display grace.
    if health == "interrupted" or status in _INTERRUPTED_STATUSES:
        meta["reason"] = "interrupted_or_terminal_governance"
        return health, meta
    if status in _TERMINAL_SUCCESS_STATUSES:
        meta["reason"] = "terminal_status"
        return health, meta
    gen = _generation_of(facts, "generation", "attempt_generation", "launch_generation", "run_generation")
    cur = _generation_of(facts, "current_generation", "active_generation", "server_generation")
    if gen and cur and gen != cur:
        meta.update(reason="stale_generation", generation=gen, current_generation=cur)
        return "interrupted", meta
    if health == "live" and status in _LIVE_STATUSES:
        meta["reason"] = "already_live_from_status"
        return health, meta

    stamped = bool(
        facts.get("stamped")
        or facts.get("has_runtime_stamp")
        or facts.get("first_progress_at")
        or facts.get("first_progress_at_ms")
        or facts.get("runtime_stamp_at")
        or facts.get("runtime_stamp_at_ms")
    )
    if stamped:
        meta["reason"] = "already_stamped"
        return health, meta

    dispatched_ms = _coerce_ms(
        facts.get("dispatched_at_ms")
        if facts.get("dispatched_at_ms") is not None
        else facts.get("launched_at_ms")
        if facts.get("launched_at_ms") is not None
        else facts.get("dispatch_at_ms")
        if facts.get("dispatch_at_ms") is not None
        else facts.get("started_at_ms")
    )
    if dispatched_ms is None:
        meta["reason"] = "no_server_dispatch_ts"
        return health, meta

    clock = _coerce_ms(now_ms if now_ms is not None else facts.get("now_ms"))
    if clock is None:
        meta["reason"] = "no_server_now_ms"
        return health, meta
    if dispatched_ms > clock:
        meta["reason"] = "future_dispatch_rejected"
        return health, meta

    gen = _generation_of(facts, "generation", "attempt_generation", "launch_generation", "run_generation")
    cur = _generation_of(facts, "current_generation", "active_generation", "server_generation")
    if not gen or not cur:
        meta["reason"] = "generation_missing"
        return health, meta
    if gen != cur:
        meta["reason"] = "stale_generation"
        meta["generation"] = gen
        meta["current_generation"] = cur
        return health, meta

    age = clock - dispatched_ms
    meta["age_ms"] = age
    if age > PRE_LEASE_DISPLAY_GRACE_MS:
        meta["reason"] = "grace_elapsed"
        return health, meta

    # Only known startup states may use display grace. Unknown is never permission.
    if health == "idle" and status in _PRE_LEASE_STATUSES:
        meta["applied"] = True
        meta["reason"] = "pre_lease_recently_started"
        return "live", meta

    meta["reason"] = "health_not_eligible_for_grace"
    return health, meta


def derive_task_runtime_health(
    *,
    timeline_entries: Iterable[Mapping[str, Any]] = (),
    run_facts: Mapping[str, Any] | None = None,
    now_ms: float | None = None,
) -> dict[str, Any]:
    """Derive live / interrupted / idle from existing facts. No new state table.

    Optional pre-lease display grace: a server-side dispatch timestamp without a
    runtime stamp may keep `health=live` for a bounded window. That signal is
    display-only (`grants_execution=False`) and must not be treated as
    permission to send, claim, or continue side effects.
    """
    facts = _as_dict(run_facts)
    status = str(facts.get("status") or facts.get("state") or facts.get("health") or "").strip().casefold()
    entries = [dict(e) for e in timeline_entries if type(e) is dict]
    entries.sort(key=lambda e: (str(e.get("at") or ""), str(e.get("id") or e.get("event_id") or "")))
    if not status and entries:
        last = entries[-1]
        status = str(last.get("status") or last.get("state") or last.get("kind") or last.get("type") or "").strip().casefold()
        # common event kinds
        if status in {"run_started", "step_started", "progress"}:
            status = "running"
        elif status in {"run_failed", "step_failed", "blocker", "approval_required"}:
            status = "blocked"
        elif status in {"run_completed", "completed", "succeeded"}:
            status = "completed"
        elif status in {"run_cancelled", "cancelled"}:
            status = "cancelled"

    if status in _LIVE_STATUSES:
        health = "live"
    elif status in _INTERRUPTED_STATUSES:
        health = "interrupted"
    elif status in _IDLE_STATUSES or not status:
        health = "idle"
    elif status in _PRE_LEASE_STATUSES:
        health = "idle"
    else:
        health = "interrupted"

    health, pre_lease = _apply_pre_lease_display_grace(
        health=health, status=status, facts=facts, now_ms=now_ms
    )

    return {
        "ok": True,
        "health": health,
        "source_status": status,
        "derived": True,
        "rules_version": RULES_VERSION,
        "pre_lease": pre_lease,
        "grants_execution": False,
        "effects": {"persisted": False},
    }


def assert_package_rewrite_fresh(
    *,
    expected_etag: str = "",
    current_etag: str = "",
    expected_version: str = "",
    current_version: str = "",
) -> dict[str, Any]:
    """Optimistic concurrency for whole-package rewrite. Stale tokens refuse."""
    exp_e = str(expected_etag or "").strip()
    cur_e = str(current_etag or "").strip()
    exp_v = str(expected_version or "").strip()
    cur_v = str(current_version or "").strip()
    if not exp_e and not exp_v:
        raise _err(
            "package_rewrite_token_missing",
            "整包重写需要 expected_etag 或 expected_version。",
            "expected",
        )
    if exp_e and cur_e and exp_e != cur_e:
        raise _err(
            "package_rewrite_stale",
            "整包重写版本冲突：etag 已过期，拒绝覆盖。",
            "etag",
            expected_etag=exp_e,
            current_etag=cur_e,
        )
    if exp_v and cur_v and exp_v != cur_v:
        raise _err(
            "package_rewrite_stale",
            "整包重写版本冲突：version 已过期，拒绝覆盖。",
            "version",
            expected_version=exp_v,
            current_version=cur_v,
        )
    # Fail closed: at least one supplied token must be compared against a
    # current value. A store that has neither the etag nor the version the
    # caller pinned cannot prove freshness (live check 2026-11-01 found that
    # expected_version with no current_version was silently accepted).
    compared = bool((exp_e and cur_e) or (exp_v and cur_v))
    if not compared:
        raise _err(
            "package_rewrite_stale",
            "整包重写无法比对当前 etag/version，拒绝覆盖。",
            "etag" if exp_e else "version",
            expected_etag=exp_e,
            current_etag=cur_e,
            expected_version=exp_v,
            current_version=cur_v,
        )
    return {
        "ok": True,
        "fresh": True,
        "etag": cur_e or exp_e,
        "version": cur_v or exp_v,
        "rules_version": RULES_VERSION,
        "effects": {"written": False},
    }


def build_task_center_view(
    *,
    project_id: str,
    actor_scope_ok: bool,
    timeline_entries: Iterable[Mapping[str, Any]] = (),
    run_facts: Mapping[str, Any] | None = None,
    now_ms: float | None = None,
    composition_record: Mapping[str, Any] | None = None,
    approval: Mapping[str, Any] | None = None,
    lifecycle_history: Mapping[str, Any] | None = None,
    experience_ledger: Mapping[str, Any] | None = None,
    knowledge_revocations: Iterable[str] = (),
    historical_explanations: Iterable[Mapping[str, Any]] = (),
    catalog_refs: Iterable[Mapping[str, Any]] = (),
    task_type_supported: bool = True,
) -> dict[str, Any]:
    """Unified read-only task center projection over existing facts."""
    if type(project_id) is not str or not project_id.strip():
        raise _err("task_center_project_invalid", "任务中心需要有效项目。", "project_id")
    if not actor_scope_ok:
        raise _err("task_center_scope_denied", "当前主体无权查看该项目任务中心。", "actor")

    entries = [dict(e) for e in timeline_entries if type(e) is dict]
    # Stable order by at + id; do not invent facts.
    entries.sort(key=lambda e: (str(e.get("at") or ""), str(e.get("id") or e.get("event_id") or "")))

    limited_reasons: list[str] = []
    if not entries:
        limited_reasons.append("no_reliable_events")
    if not task_type_supported:
        limited_reasons.append("task_type_limited_support")

    composition = explain_composition(composition_record, approval)
    if composition.get("limited"):
        limited_reasons.append(str(composition.get("reason") or "composition_limited"))

    ledger = _as_dict(experience_ledger)
    versions = list(ledger.get("versions") or [])
    candidates = [
        v for v in versions
        if type(v) is dict and str(v.get("status") or "") in {"candidate", "needs_human_review"}
    ]

    revoked = [str(x) for x in knowledge_revocations if str(x).strip()]
    history_notes: list[dict[str, str]] = []
    block_new: list[str] = []
    for ref in revoked:
        impact = knowledge_revoke_impact(
            revoked_ref=ref,
            catalog_refs=catalog_refs,
            historical_explanations=historical_explanations,
        )
        block_new.extend(impact.get("block_new_refs") or [])
        for note in impact.get("history") or []:
            history_notes.append(
                {"id": str(note.get("id") or ""), "explanation": str(note.get("explanation") or "该知识来源已失效。")}
            )

    support = "full" if not limited_reasons else "limited"
    hist = _as_dict(lifecycle_history)
    runtime_health = derive_task_runtime_health(timeline_entries=entries, run_facts=run_facts, now_ms=now_ms)
    return {
        "ok": True,
        "project_id": project_id,
        "schema_version": SCHEMA_VERSION,
        "rules_version": RULES_VERSION,
        "support": support,
        "limited_reasons": limited_reasons,
        "timeline": {"entries": entries, "count": len(entries)},
        "composition": composition,
        "lifecycle": {
            "present": bool(hist),
            "package_id": hist.get("package_id"),
            "trials": hist.get("trials") if isinstance(hist.get("trials"), list) else [],
        },
        "experience": {
            "adopted_version_id": ledger.get("adopted_version_id"),
            "candidates_count": len(candidates),
            "wording": _CANDIDATE_WORDING,
        },
        "knowledge": {
            "revoked_refs": revoked,
            "block_new_refs": sorted(set(block_new)),
            "history_notes": history_notes,
            "content_included": False,
        },
        "runtime_health": runtime_health,
        "effects_produced": False,
    }



def apply_experience_promotion_gate(
    experience_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Default-off 17E gate for formal adopt paths.

    ``None`` / empty → skip (legacy adopt unchanged). Non-empty must carry
    ``candidate`` and ``regression_results``; then ``assert_experience_promotion_allowed``
    runs. Never adopts or writes (``adopted_changed`` stays False here).
    """
    if experience_gate is None or experience_gate == {}:
        return {
            "ok": True,
            "skipped": True,
            "reason": "gate_off",
            "rules_version": RULES_VERSION,
            "effects": {"adopted_changed": False, "publish_performed": False},
        }
    if type(experience_gate) is not dict:
        raise _err("experience_gate_invalid", "经验晋升门禁参数无效。")
    if "regression_results" not in experience_gate or experience_gate.get("candidate") is None:
        raise _err(
            "experience_gate_incomplete",
            "启用经验晋升门禁时必须同时提供 candidate 与 regression_results。",
        )
    result = assert_experience_promotion_allowed(
        candidate=experience_gate["candidate"],  # type: ignore[arg-type]
        adopted_version_id=experience_gate.get("adopted_version_id"),  # type: ignore[arg-type]
        regression_results=experience_gate.get("regression_results") or (),
    )
    out = dict(result)
    out["skipped"] = False
    out["reason"] = "gate_enforced"
    return out


def task_center_read_api(
    *,
    enabled: bool = False,
    project_id: str = "",
    actor_scope_ok: bool = False,
    timeline_entries: Iterable[Mapping[str, Any]] = (),
    run_facts: Mapping[str, Any] | None = None,
    now_ms: float | None = None,
    composition_record: Mapping[str, Any] | None = None,
    approval: Mapping[str, Any] | None = None,
    lifecycle_history: Mapping[str, Any] | None = None,
    experience_ledger: Mapping[str, Any] | None = None,
    knowledge_revocations: Iterable[str] = (),
    historical_explanations: Iterable[Mapping[str, Any]] = (),
    catalog_refs: Iterable[Mapping[str, Any]] = (),
    task_type_supported: bool = True,
) -> dict[str, Any]:
    """Default-off read-only task-center API. No ledger/Runtime writes."""
    if not enabled:
        return {
            "ok": True,
            "skipped": True,
            "reason": "api_off",
            "rules_version": RULES_VERSION,
            "effects_produced": False,
        }
    view = build_task_center_view(
        project_id=project_id,
        actor_scope_ok=actor_scope_ok,
        timeline_entries=timeline_entries,
        run_facts=run_facts,
        now_ms=now_ms,
        composition_record=composition_record,
        approval=approval,
        lifecycle_history=lifecycle_history,
        experience_ledger=experience_ledger,
        knowledge_revocations=knowledge_revocations,
        historical_explanations=historical_explanations,
        catalog_refs=catalog_refs,
        task_type_supported=task_type_supported,
    )
    out = dict(view)
    out["skipped"] = False
    out["reason"] = "api_enabled"
    return out
