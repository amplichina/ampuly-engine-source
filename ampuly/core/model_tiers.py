"""档位表: which model a step runs on, and why -- model routing phase 1, E1.

The implementation lives here. ``ampuly.plugins.office_ui.model_tiers`` re-exports it.

Pure. No imports from ``ampuly.core.config``, no environment, no disk, no clock. Everything it needs arrives in the
``plan`` argument, which is a frozen copy of the owner's settings taken when an order was launched (E6). That is
the whole point: a run must answer 「这一步用了哪个模型，为什么」 the same way a month later, even if the settings
have changed since, so the answer may not depend on anything that can move underneath it.

The promise this module has to keep (spec R1): **with an empty tier table, every answer is byte-for-byte what
`LLMProvider._select_model` answers today.** Two of the five steps below simply never fire when there are no rows,
and the remaining three are today's three.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

TIERS: tuple[str, ...] = ("light", "standard", "strong")
TIER_ORDER: dict[str, int] = {"light": 0, "standard": 1, "strong": 2}

# What an AI work step runs on when nobody said otherwise -- the owner's 「不确认就是标准」. It is never 「light」:
# a step that produces work for a customer is not the place to save money by default.
DEFAULT_WORK_TIER = "standard"

# The keys a tier's own parameters may carry. Anything else in the yaml is ignored rather than guessed at.
TIER_PARAM_KEYS: tuple[str, ...] = ("thinking", "window_cap", "tool_loop_trial")


def normalize_tier(value: Any) -> str:
    """One of :data:`TIERS`, or ``""`` for anything else -- including ``None`` and a tier spelled differently."""
    name = str(value or "").strip().lower()
    return name if name in TIER_ORDER else ""


def plan_from_config(llm_config: Any) -> dict[str, Any]:
    """The frozen plan: a copy of what the owner's settings said, plus a digest of it.

    The one function here that touches something outside its arguments, and only to read fields off it. The digest
    is over the plan without ``plan_digest`` itself, so two plans built from the same settings agree.
    """
    plan: dict[str, Any] = {
        "default_model": str(getattr(llm_config, "default_model", "") or ""),
        "routing": {str(k): str(v) for k, v in dict(getattr(llm_config, "routing", {}) or {}).items()},
        "tiers": {normalize_tier(k): str(v) for k, v in dict(getattr(llm_config, "tiers", {}) or {}).items()
                  if normalize_tier(k) and str(v or "").strip()},
        "tier_params": {normalize_tier(k): {p: v for p, v in dict(params or {}).items() if p in TIER_PARAM_KEYS}
                        for k, params in dict(getattr(llm_config, "tier_params", {}) or {}).items()
                        if normalize_tier(k)},
        "task_tiers": {str(k): normalize_tier(v)
                       for k, v in dict(getattr(llm_config, "task_tiers", {}) or {}).items()
                       if normalize_tier(v)},
        "residency": str(getattr(llm_config, "residency", "") or ""),
        "work_window_cap": int(getattr(llm_config, "work_window_cap", 0) or 0),
        "no_training_optout": {str(k): str(v)
                               for k, v in dict(getattr(llm_config, "no_training_optout", {}) or {}).items()},
    }
    plan["plan_digest"] = plan_digest(plan)
    return plan


def plan_digest(plan: dict[str, Any]) -> str:
    """sha256 over the canonical plan, without the digest itself or when it was pinned."""
    body = {k: v for k, v in dict(plan or {}).items() if k not in {"plan_digest", "pinned_at"}}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def tier_of_model(plan: dict[str, Any], model: str) -> str:
    """The highest tier in the plan whose model is this one; ``"strong"`` when no tier holds it.

    A model the owner pinned onto a step themselves is outside the plan's judgement, so it counts as at least as
    strong as anything the plan could weigh it against. The gate floor (E8), the 「用了哪个模型」 record, the
    review card and the approval receipt all read this one answer, so they cannot disagree.
    """
    wanted = str(model or "").strip()
    if not wanted:
        return ""
    holders = [tier for tier in TIERS if str(dict(plan.get("tiers") or {}).get(tier, "") or "") == wanted]
    if not holders:
        return "strong"
    return max(holders, key=lambda tier: TIER_ORDER[tier])


def resolve_model(plan: dict[str, Any], *, kind: str = "", tier: str = "", node_model: str = "",
                  is_work_step: bool = False) -> dict[str, str]:
    """Which model, and why. ``{"model", "source", "tier", "refused"}``.

    Highest first:

    1. ``node``       -- the step names its own model.
    2. ``kind_pin``   -- this task type has a pin of its own in ``routing``.
    3. ``tier``       -- the step's tier, the kind's tier, or 「标准」 for an AI work step.
    4. ``quick_tasks``-- the shared pin the six task kinds fall back to.
    5. ``default``    -- the global default model.

    Steps 2 and 4 are the two halves of ``task_kinds.routing_chain(kind)`` and nothing besides: a task type
    outside the six has a one-key chain and so can never fall through onto the ``quick_tasks`` pin, and an AI work
    step has an empty chain, so neither fires and it lands on step 5 -- which is what ``_select_model(None)``
    answers today, because a falsy task type never reads ``routing`` at all.

    ``refused`` is ``"tier_not_set"`` only when a tier was asked for **by name** and the plan has no row for it.
    The unnamed 「标准」 of a work step is not a refusal: with no row it falls through, which is what keeps an
    empty table silent.
    """
    from ampuly.llm.task_kinds import routing_chain

    routing = dict(plan.get("routing") or {})
    tiers = dict(plan.get("tiers") or {})
    task_tiers = dict(plan.get("task_tiers") or {})
    default_model = str(plan.get("default_model", "") or "")

    named = str(node_model or "").strip()
    if named:
        return {"model": named, "source": "node", "tier": tier_of_model(plan, named), "refused": ""}

    chain = routing_chain(str(kind or ""))
    if chain:
        first = routing.get(chain[0], "")
        if first:
            return {"model": str(first), "source": "kind_pin", "tier": "", "refused": ""}

    # Which tier applies, and whether anybody named it. A name that misses is an error to report; the default
    # work tier missing is not, because nobody asked for it by name.
    asked = normalize_tier(tier) or normalize_tier(task_tiers.get(str(kind or ""), ""))
    wanted = asked or (DEFAULT_WORK_TIER if is_work_step else "")
    if wanted:
        found = str(tiers.get(wanted, "") or "")
        if found:
            return {"model": found, "source": "tier", "tier": wanted, "refused": ""}
        if asked:
            return {"model": "", "source": "", "tier": asked, "refused": "tier_not_set"}

    for key in chain[1:]:
        shared = routing.get(key, "")
        if shared:
            return {"model": str(shared), "source": "quick_tasks", "tier": "", "refused": ""}

    return {"model": default_model, "source": "default", "tier": "", "refused": ""}


def resolve_gate_model(plan: dict[str, Any], *, work_tier: str = "", risk: str = "") -> dict[str, str]:
    """Which model may judge work that ran on ``work_tier``.

    The owner's rule, in three clauses: never below 「标准」; never below the work it is judging; 「强」 when the
    risk is high. Then the **lowest** tier at or above that floor which the table actually holds -- judging is
    not a reason to spend more than the rule asks for.

    ``work_tier == ""`` means 「标准」, not 「unknown, so anything goes」: an old task with no record of what it
    ran on must not lower the bar.

    With an empty table this answers exactly what an ordinary resolution answers, and ``refused`` stays empty --
    the rule only begins refusing once the owner has filled the table in. When the table is filled but holds no
    tier at or above the floor, the answer is ``gate_tier_unavailable``, which every caller turns into 「交给人」.
    Never into a pass.
    """
    tiers = dict(plan.get("tiers") or {})
    floor = max(TIER_ORDER[DEFAULT_WORK_TIER], TIER_ORDER.get(normalize_tier(work_tier), TIER_ORDER[DEFAULT_WORK_TIER]))
    if str(risk or "").strip().lower() in {"high", "critical"}:
        floor = TIER_ORDER["strong"]
    for tier in TIERS:
        if TIER_ORDER[tier] < floor:
            continue
        found = str(tiers.get(tier, "") or "")
        if found:
            return {"model": found, "source": "gate_tier", "tier": tier, "refused": ""}
    if tiers:
        # The owner has a table, and nothing in it is good enough to judge this. A person decides.
        return {"model": "", "source": "", "tier": "", "refused": "gate_tier_unavailable"}
    # No table at all: today's behaviour, which is what R1 promises for gatekeeping too.
    return resolve_model(plan)


def tier_params(plan: dict[str, Any], tier: str) -> dict[str, Any]:
    """This tier's own parameters, with the plan's window cap filled in where the tier sets none."""
    name = normalize_tier(tier)
    params = dict(dict(plan.get("tier_params") or {}).get(name, {}) or {}) if name else {}
    cap = int(params.get("window_cap", 0) or 0)
    return {
        "thinking": bool(params.get("thinking", False)),
        "window_cap": cap if cap > 0 else int(plan.get("work_window_cap", 0) or 0),
        "tool_loop_trial": str(params.get("tool_loop_trial", "") or ""),
    }


def suggest_tier(node: dict[str, Any] | None) -> dict[str, str]:
    """What to suggest for this step in the builder, and the sentence that says why.

    Pure, and only a suggestion: it fills the field, the owner decides. 「light」 is never suggested for a work
    step -- a step whose output goes to a customer is not where to save money by default.
    """
    step = dict(node or {})
    if str(step.get("type", "") or "") != "ai_do":
        return {"tier": "", "reason_zh": "不是 AI 步骤，不用选档", "reason_en": "Not an AI step; no tier needed"}

    deliverables = step.get("deliverables") or []
    gated = (str(step.get("gate", "") or "") == "human_required"
             or bool(str(step.get("gate_to", "") or "").strip())
             or (bool(deliverables) and bool(str(step.get("standard", "") or "").strip())))
    if gated:
        return {"tier": "strong", "reason_zh": "有质量门、要交客户的交付物 → 建议强",
                "reason_en": "A quality gate and a deliverable for the customer → strong"}
    if str(step.get("connector", "") or "").strip():
        return {"tier": "strong", "reason_zh": "要在浏览器里操作，出错代价高 → 建议强",
                "reason_en": "Works a browser, where a mistake is expensive → strong"}
    if str(step.get("invoke", "") or "").strip():
        return {"tier": "standard", "reason_zh": "要调起子流程 → 建议标准",
                "reason_en": "Calls a sub-flow → standard"}
    return {"tier": "standard", "reason_zh": "普通 AI 步骤 → 建议标准",
            "reason_en": "An ordinary AI step → standard"}
