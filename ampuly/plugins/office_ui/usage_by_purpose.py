"""What the model calls of a project (or of one order) were FOR, from the rows that already exist.

The question this answers is the owner's : of everything an order spends on model calls, how much goes to
JUDGEMENT -- a gate judge, a review card, a delivery assessment, a meeting vote: calls whose answer is one of a few
choices -- and how much of that is output tokens. That share is the ceiling on what a judgement-only model could save,
measured on our own orders rather than read off a vendor's benchmark, and it is the number the decision about such a
model waits for (the module contract).

Nothing new is recorded. Every call is already filed against a task, and the task already says what it is: a work
item, a hidden review card, a role prompt of a named kind, a meeting turn, an accounting-only identity with a purpose.
This module reads those marks. What it cannot see is said in ``LIMITS`` and travels with every answer.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

WORK = "work"
JUDGEMENT = ("gate_judge", "delivery_assessment", "review_card", "meeting_turn", "assessment_other")
ORDER = (WORK, *JUDGEMENT, "report_card", "memory", "staffing", "secretary", "other")

LIMITS = (
    "A model review of an approval, and the folding of a long history, are filed against the turn that caused them: "
    "they are inside 'work' here, not listed apart.",
    "A turn run by an external agent is counted as one call; the requests the agent made inside it go to the "
    "operator's own account and are not on this ledger.",
)

_ROLE_PROMPTS = {"gate_harness_judge": "gate_judge", "ceo_pre_delivery_assessment": "delivery_assessment"}


def purpose_of(metadata: Mapping[str, Any] | None) -> str:
    data = dict(metadata or {})
    if data.get("accounting_only") is True:
        purpose = str(data.get("accounting_purpose", "") or "")
        if purpose.startswith("recruiter"):
            return "staffing"
        if purpose.startswith("secretary"):
            return "secretary"
        if purpose.startswith(("finalize_agent_memory", "runtime_session_memory")):
            return "memory"
        return "other"
    kind = str(data.get("runtime_auxiliary_kind", "") or "")
    if kind == "role_prompt":
        return _ROLE_PROMPTS.get(str(data.get("prompt_kind", "") or ""), "assessment_other")
    if kind == "meeting_turn":
        return "meeting_turn"
    if data.get("review_execution_work_item"):
        return "review_card"
    if data.get("report_execution_work_item"):
        return "report_card"
    if data.get("work_item_runtime") or data.get("execution_mode"):
        return WORK
    return "other"


def _empty() -> dict[str, Any]:
    return {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0}


def summarize(cost_by_task: Mapping[str, Mapping[str, Any]], tasks: Iterable[Any], *, order_id: str = "") -> dict[str, Any]:
    """Totals per purpose over the tasks given (narrowed to one order when asked), and the judgement share.

    A task with recorded cost and no task row is counted under ``unattributed`` rather than dropped: a total that
    quietly leaves rows out reads as the whole bill.
    """
    wanted = str(order_id or "").strip()
    by_purpose: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for task in tasks:
        task_id = str(getattr(task, "id", "") or "")
        metadata = dict(getattr(task, "metadata", {}) or {})
        seen.add(task_id)
        if wanted and str(metadata.get("order_id", "") or "") != wanted:
            continue
        spent = cost_by_task.get(task_id)
        if not spent:
            continue
        row = by_purpose.setdefault(purpose_of(metadata), _empty())
        for key in ("calls", "tokens_in", "tokens_out"):
            row[key] += int(spent.get(key, 0) or 0)
        row["cost"] += float(spent.get("cost", 0.0) or 0.0)
    unattributed = _empty()
    if not wanted:
        for task_id, spent in cost_by_task.items():
            if task_id not in seen:
                for key in ("calls", "tokens_in", "tokens_out"):
                    unattributed[key] += int(spent.get(key, 0) or 0)
                unattributed["cost"] += float(spent.get("cost", 0.0) or 0.0)
    total = _empty()
    judgement = _empty()
    for purpose, row in by_purpose.items():
        for key in ("calls", "tokens_in", "tokens_out", "cost"):
            total[key] += row[key]
            if purpose in JUDGEMENT:
                judgement[key] += row[key]
    all_tokens = total["tokens_in"] + total["tokens_out"]
    judgement_tokens = judgement["tokens_in"] + judgement["tokens_out"]
    return {
        "order_id": wanted,
        "by_purpose": [{"purpose": purpose, **by_purpose[purpose]} for purpose in ORDER if purpose in by_purpose],
        "total": total,
        "judgement": {
            **judgement,
            # None, not zero, when nothing was spent: "0%" of nothing is not a measurement.
            "share_of_tokens": (judgement_tokens / all_tokens) if all_tokens else None,
            "output_share_of_judgement": (judgement["tokens_out"] / judgement_tokens) if judgement_tokens else None,
            "share_of_calls": (judgement["calls"] / total["calls"]) if total["calls"] else None,
        },
        "unattributed": unattributed,
        "limits": list(LIMITS),
    }
