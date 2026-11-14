"""The six kinds 「快速任务」 was split into, and what each falls back to -- model routing phase 1, E2.

Pure, and deliberately tiny: it imports nothing from ``ampuly``. The kernel's one model selector walks the chain this
module describes, and the kernel may not import a plugin, so the chain lives here rather than beside the tier
table it is read with.

The shape that makes phase 1 safe to land: a kind with no setting of its own still resolves through
``quick_tasks``, so a config written before phase 1 answers exactly as it did. A task type that is **not** one of
the six has a chain of length one, so it can never fall through onto the shared pin -- 「视觉定位」 and the coding
probe keep their own answers. A call with no task type at all has an empty chain, which is today's behaviour too:
a falsy task type never reads ``routing``.
"""
from __future__ import annotations

QUICK_TASKS = "quick_tasks"

KINDS: tuple[str, ...] = ("chore", "review", "gate_judge", "assessment", "agent_select", "master_control")

FALLBACK: dict[str, str] = {kind: QUICK_TASKS for kind in KINDS}


def routing_chain(task_type: str | None) -> tuple[str, ...]:
    """The routing keys to try for this task type, most specific first.

    ``"review"`` → ``("review", "quick_tasks")``; ``"quick_tasks"`` → ``("quick_tasks",)``;
    ``"vision_grounding"`` → ``("vision_grounding",)``; ``None`` or ``""`` → ``()``.
    """
    key = str(task_type or "").strip()
    if not key:
        return ()
    nxt = FALLBACK.get(key, "")
    return (key, nxt) if nxt else (key,)
