"""下单时把模型计划锁住 -- model routing phase 1, E6.

The implementation lives here. ``ampuly.plugins.office_ui.order_model_plan`` re-exports it.

An order is placed under the settings that were in force when it was placed. If a step read the live settings at
the moment it ran, the same order could be half on one plan and half on another -- the owner changes a tier at
lunchtime and the afternoon's steps quietly cost differently from the morning's, with nothing in the record saying
so. So the plan is frozen at claim time, written into the launch payload, pinned onto the run task, inherited by
every work item, and recorded as one event the owner can read back.

The precedent this follows exactly is the order's allowance: granted before the run exists, guaranteed at
dispatch, pinned onto the run task, and a run that cannot be pinned does not start.
"""
from __future__ import annotations

from typing import Any

from ampuly.llm.task_model_scope import SCHEMA, plan_from_task, plan_scope_for
from ampuly.core.model_tiers import plan_from_config

# Compatibility exports above retain existing application imports. The
# Harness reads frozen policy from ampuly.llm, never through the office UI.
PINNED_EVENT = "model_plan.pinned"


def build_plan(llm_config: Any, *, source: str = "global", pinned_at: str = "") -> dict[str, Any]:
    """The plan this order will run under, from the settings in force right now.

    ``default_model`` and ``routing`` are part of it on purpose: without them the fall-through at the end of
    ``resolve_model`` would have to read the live settings, which is the very thing freezing the plan prevents.
    """
    plan = plan_from_config(llm_config)
    plan["schema"] = SCHEMA
    plan["source"] = str(source or "global")
    if pinned_at:
        plan["pinned_at"] = str(pinned_at)
    # The digest is recomputed after schema/source are in, so two plans agree only when everything agrees.
    from ampuly.core.model_tiers import plan_digest

    plan["plan_digest"] = plan_digest(plan)
    return plan


def event_note(plan: dict[str, Any]) -> dict[str, Any]:
    """What the pinned event records: enough to answer 「这单是按什么设置跑的」 without re-reading the task."""
    found = dict(plan or {})
    return {
        "plan_digest": str(found.get("plan_digest", "") or ""),
        "tiers": dict(found.get("tiers") or {}),
        "task_tiers": dict(found.get("task_tiers") or {}),
        "residency": str(found.get("residency", "") or ""),
        "work_window_cap": int(found.get("work_window_cap", 0) or 0),
        "source": str(found.get("source", "") or ""),
    }


def gate_model_for(task: Any, *, risk: str = "", live_config: Any = None) -> dict[str, str]:
    """Which model may judge this work, and why -- or ``gate_tier_unavailable``.

    The plan is the order's when one is in scope, and a plan built from the live settings otherwise: 秘书 and the
    console's own reviews are not an order's work but are still 把关, and they are held to the same floor.
    """
    from ampuly.llm.provider import current_model_plan
    from ampuly.core.model_tiers import resolve_gate_model

    plan = current_model_plan()
    if plan is None:
        plan = build_plan(live_config) if live_config is not None else {}
    work_tier = str(dict(getattr(task, "metadata", {}) or {}).get("work_tier", "") or "")
    if not work_tier:
        work_tier = str(dict(dict(getattr(task, "metadata", {}) or {}).get("model_route") or {})
                        .get("tier", "") or "")
    return resolve_gate_model(dict(plan or {}), work_tier=work_tier, risk=str(risk or ""))
