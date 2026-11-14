"""How an auxiliary model call gets onto the bill.

The runtime records what a turn costs. Everything else the product asks a
model -- picking an agent, reviewing an approval, judging a gate, attributing
feedback, summarising a meeting, compacting a history -- went to the provider,
returned an answer, and appeared in no total. The plan says it in one line
(§7, rule 7): the master conversation, sub-tasks, compaction and every other
auxiliary model call are metered; you cannot arrive at a number by summing
the worker fields alone.

Measured on 2026-09-10: of eleven auxiliary call sites in `ampuly/`, exactly one
passed a meter. The other ten spent invisibly, and the product reported the
total as if it were the total.

Two helpers, both deliberately small.

`meter_for` builds the callback the provider hands its usage to. It returns
None when there is no tracker or no id to file under -- the rule stated once
here instead of ten times, and stated the same way each time. A meter that
invented an id would be worse than none: the row would read as recorded, join
to nothing, and the total would still be wrong while now looking accounted
for.

`metered_simple_chat` passes the keyword only when there is a meter. That is
not style. A provider stub written before `meter` existed raises TypeError on
an unexpected keyword; the JSON retry helper reads a TypeError as a transport
failure and retries three times; and the gate harness, out of retries,
defaults to pass. That chain turned one unconditional keyword into six red
tests and a quality gate that stopped judging, on 2026-09-10. The conditional
is the fix, and it belongs in one place.
"""

from __future__ import annotations

from typing import Any, Callable


def meter_for(
    tracker: Any,
    *,
    task_id: str = "",
    agent_id: str = "",
    org_id: str = "",
) -> Callable[..., Any] | None:
    """A callback that files one model call's usage, or None.

    None means "this call is not metered", and the caller passing it along
    keeps the old behaviour exactly -- unmetered, and visibly so, rather than
    metered to somewhere nobody looks.
    """

    if tracker is None:
        return None
    key = str(task_id or "").strip()
    if not key:
        return None
    who = str(agent_id or "").strip()
    org = str(org_id or "").strip()

    async def _meter(*, model: str, tokens_in: int, tokens_out: int, cost: float) -> None:
        from ampuly.layer6_observability.cost_tracker import CostEntry

        await tracker.record(CostEntry(
            task_id=key,
            agent_id=who or None,
            org_id=org or None,
            model=str(model or ""),
            tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0),
            cost=float(cost or 0.0),
        ))

    return _meter


def meter_for_task(tracker: Any, task: Any, *, agent_id: str = "") -> Callable[..., Any] | None:
    """The usual case: file the call against the task that caused it.

    The task carries who was assigned and which org it belongs to, so a row
    written this way answers "who spent this" without the call site having to
    remember to say.
    """

    if task is None:
        return None
    return meter_for(
        tracker,
        task_id=str(getattr(task, "id", "") or ""),
        agent_id=agent_id or str(getattr(task, "assigned_to", "") or ""),
        org_id=str(getattr(task, "org_id", "") or ""),
    )


async def meter_for_auxiliary(
    store: Any, *, project_id: str, session_id: str, purpose: str,
) -> Callable[..., Any]:
    """Persist an accounting-only identity before an auxiliary model call.

    A synthetic cost key without its task row disappears from project joins.
    Reuse a stable identity without rewriting an existing task or its scope.
    This grants no execution workspace and performs no model call itself.
    """
    import hashlib
    import json

    from ampuly.core.models import Task, TaskStatus
    from ampuly.layer6_observability.cost_tracker import CostTracker

    if store is None or not project_id or not purpose:
        raise ValueError("auxiliary accounting requires store, project and purpose")
    if session_id:
        session = await store.get_session(session_id)
        if session is None or session.project_id != project_id:
            raise ValueError("session accounting project mismatch or session missing")
    key = hashlib.sha256(json.dumps(
        [project_id, session_id, purpose], separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    task_id = "accounting-" + key
    task = await store.get_task(task_id)
    if task is None:
        task = Task(
            id=task_id, title="Auxiliary model accounting", project_id=project_id,
            session_id=session_id, status=TaskStatus.DONE,
            metadata={"accounting_only": True, "accounting_purpose": purpose,
                      "runtime_auxiliary_task": True, "runtime_auxiliary_kind": "model_accounting"},
        )
        await store.save_task(task)
    elif (task.project_id != project_id or task.session_id != session_id
          or (task.metadata or {}).get("accounting_only") is not True
          or (task.metadata or {}).get("accounting_purpose") != purpose):
        raise ValueError("session accounting identity mismatch")
    return meter_for(CostTracker(store), task_id=task_id)


async def metered_simple_chat(
    llm: Any,
    *,
    prompt: str,
    system: str | None = None,
    task_type: str | None = None,
    meter: Any = None,
) -> str:
    """`llm.simple_chat`, passing the meter only when there is one.

    The conditional is load-bearing. A provider written against the older
    signature raises TypeError on an unexpected keyword; callers that wrap
    model calls in retry-on-exception read that as a transport failure and
    burn their retries; and at least one of them -- the gate harness --
    defaults to pass when it runs out. Passing `meter=None` unconditionally
    is therefore not a no-op, and finding that out took six red tests.
    """

    return await llm.simple_chat(
        prompt=prompt,
        system=system,
        task_type=task_type,
        **({"meter": meter} if meter is not None else {}),
    )
