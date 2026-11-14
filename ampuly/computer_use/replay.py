"""Deterministic replay of an admitted candidate through the product's own browser tools (4.3 X07, fourth slice).

A run exists only as a RunBinding (admission, scope, Effect claim; G12). Each
step runs only when the page's structure digest is the one the candidate
expects (drift stops the run, C19); after the step the page must have become
what the candidate expects (an unexpected result stops it). Typed text is not
in a candidate -- a demo holds lengths -- so the caller supplies the inputs
for this run by step. A submit step is not clicked here: the tools propose it
into the owner's approval inbox as they do for the agent, and the run pauses
there. Nothing is guessed and nothing is retried.
"""
from __future__ import annotations

import asyncio
from typing import Any

from ampuly.computer_use.workflow_contracts import CandidateStep, ReplayCursor, RunBinding, WorkflowRefused


class ReplayStopped(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def _selector(step: CandidateStep) -> str:
    target = step.target
    if target.get("ref"):
        return f"ref:{target['ref']}"
    return str(target.get("selector", "") or "")


async def _perform(step: CandidateStep, *, tools: Any, task: Any, inputs: dict[int, str]) -> dict[str, Any]:
    """One candidate step as one tool call. The tools keep every policy they have for the agent."""
    target = step.target
    if step.kind == "observe":
        if target.get("url"):
            return await tools.browser_navigate(url=str(target["url"]), task=task)
        return await tools.browser_snapshot(task=task)
    if step.kind == "type_draft":
        if step.sequence not in inputs:
            raise ReplayStopped("input_missing", f"step {step.sequence} types text this run did not supply")
        return await tools.browser_type(selector=_selector(step), text=str(inputs[step.sequence]), task=task)
    if step.kind == "select_candidate":
        return await tools.browser_click(selector=_selector(step), task=task)
    if step.kind == "submit":
        # The same road the agent takes: a submit is proposed, never clicked here.
        return await tools.browser_click(selector=_selector(step), task=task)
    if step.kind == "scroll":
        return await tools.browser_scroll(amount=int(target.get("amount", 800) or 800), direction=str(target.get("direction", "down") or "down"), task=task)
    if step.kind == "switch_tab":
        return await tools.browser_switch_tab(index=int(target.get("index", 0) or 0), task=task)
    if step.kind == "wait":
        await asyncio.sleep(float(target.get("seconds", 1) or 1))
        return {"waited": True}
    raise ReplayStopped("step_kind_not_replayable", step.kind)


async def replay_candidate(
    *,
    run: RunBinding,
    task: Any,
    tools: Any,
    runtime_for: Any,
    inputs: dict[int, str] | None = None,
    run_journal: Any = None,
    binding_key: str = "",
    paused_effect: Any = None,
) -> dict[str, Any]:
    """Run or safely recover one candidate replay.

    With a run journal, confirmed steps resume on the same run identity after
    restart. A step that was durably started but not confirmed is reported as
    unknown and is never executed again automatically.
    """
    cursor = ReplayCursor(run)
    supplied = {int(k): str(v) for k, v in dict(inputs or {}).items()}
    performed: list[dict[str, Any]] = []
    if run_journal is not None:
        state = run_journal.begin(
            run,
            task_id=str(getattr(task, "id", "") or ""),
            binding_key=binding_key,
        )
        cursor.position = state.position
        performed.extend(
            {"sequence": step.sequence, "kind": step.kind, "restored": True}
            for step in run.candidate.steps[:state.position]
        )
        if state.status == "complete":
            return {"status": "complete", "steps": state.position, "performed": performed,
                    "run_id": run.run_id, "restored": True}
        if state.status == "stopped":
            return {"status": "stopped", "reason": state.reason, "at_step": state.position + 1,
                    "performed": performed, "run_id": run.run_id, "restored": True}
        if state.status == "paused_for_approval":
            if paused_effect is None:
                return {"status": "paused_for_approval", "at_step": state.position + 1,
                        "effect_id": state.effect_id, "checkpoint_id": state.checkpoint_id,
                        "performed": performed, "run_id": run.run_id, "restored": True}
            effect_state = run_journal.assert_effect(state, paused_effect)
            if effect_state in {"rejected", "failed", "outcome_unknown"}:
                reason = f"submit_{effect_state}"
                run_journal.append(run.run_id, "stopped", position=state.position, reason=reason)
                return {"status": "stopped", "reason": reason, "at_step": state.position + 1,
                        "effect_id": state.effect_id, "performed": performed,
                        "run_id": run.run_id, "restored": True}
            if effect_state != "applied":
                return {"status": "paused_for_approval", "at_step": state.position + 1,
                        "effect_id": state.effect_id, "checkpoint_id": state.checkpoint_id,
                        "performed": performed, "run_id": run.run_id, "restored": True}
            step = run.candidate.steps[state.position]
            if step.kind != "submit":
                raise WorkflowRefused("replay_effect_step_mismatch", str(step.sequence))
            runtime = await runtime_for(task)
            try:
                await tools.browser_snapshot(task=task)
            except Exception as exc:  # noqa: BLE001 - applied effect remains paused until independently observed
                return {"status": "awaiting_verification", "reason": "post_effect_observation_failed",
                        "detail": str(exc)[:200], "at_step": step.sequence, "effect_id": state.effect_id,
                        "performed": performed, "run_id": run.run_id, "restored": True}
            after = str(getattr(runtime, "last_snapshot_digest", "") or "")
            if after != step.expected_after_hash:
                run_journal.append(run.run_id, "stopped", position=state.position, reason="post_effect_drift")
                return {"status": "stopped", "reason": "post_effect_drift", "at_step": step.sequence,
                        "effect_id": state.effect_id, "performed": performed,
                        "run_id": run.run_id, "restored": True}
            cursor.position = state.position + 1
            run_journal.append(run.run_id, "step_confirmed", step=step.sequence, position=cursor.position)
            performed.append({"sequence": step.sequence, "kind": step.kind, "effect_id": state.effect_id, "restored": True})
        if state.status == "outcome_unknown":
            return {"status": "outcome_unknown", "reason": state.reason,
                    "at_step": state.in_flight_step or state.position + 1,
                    "performed": performed, "run_id": run.run_id, "restored": True}
    runtime = await runtime_for(task)
    while not cursor.complete:
        before = str(getattr(runtime, "last_snapshot_digest", "") or "")
        try:
            step = cursor.next_step(before)
        except WorkflowRefused as exc:
            if run_journal is not None:
                run_journal.append(run.run_id, "stopped", position=cursor.position, reason=exc.reason)
            return {"status": "stopped", "reason": exc.reason, "at_step": cursor.position + 1, "detail": str(exc)[:200],
                    "performed": performed, "run_id": run.run_id}
        if run_journal is not None:
            run_journal.append(run.run_id, "step_started", position=cursor.position, step=step.sequence)
        try:
            result = await _perform(step, tools=tools, task=task, inputs=supplied)
        except ReplayStopped as exc:
            cursor.stopped = exc.reason
            if run_journal is not None:
                run_journal.append(run.run_id, "stopped", position=cursor.position, reason=exc.reason)
            return {"status": "stopped", "reason": exc.reason, "at_step": step.sequence, "detail": str(exc)[:200],
                    "performed": performed, "run_id": run.run_id}
        except Exception as exc:  # noqa: BLE001 - a refused or failed tool call stops the run with its reason
            cursor.stopped = f"tool_refused_at_step_{step.sequence}"
            reason = str(getattr(exc, "reason", "") or type(exc).__name__)
            if run_journal is not None:
                run_journal.append(run.run_id, "stopped", position=cursor.position, reason=reason)
            return {"status": "stopped", "reason": reason, "at_step": step.sequence, "detail": str(exc)[:200],
                    "performed": performed, "run_id": run.run_id}
        if isinstance(result, dict) and result.get("status") == "awaiting_approval":
            # The submit went to the owner's inbox; the run waits there, and resumes only as a new run
            # from a fresh observation once the effect has an outcome.
            performed.append({"sequence": step.sequence, "kind": step.kind, "effect_id": result.get("effect_id", "")})
            if run_journal is not None:
                run_journal.append(
                    run.run_id, "paused_for_approval", position=cursor.position,
                    step=step.sequence, effect_id=result.get("effect_id", ""),
                    checkpoint_id=result.get("checkpoint_id", ""),
                )
            return {"status": "paused_for_approval", "at_step": step.sequence, "effect_id": result.get("effect_id", ""),
                    "checkpoint_id": result.get("checkpoint_id", ""), "performed": performed, "run_id": run.run_id}
        if step.kind == "submit" and isinstance(result, dict) and result.get("status"):
            # A terminal/unknown proposal result is not a browser action.  In
            # particular, never advance the replay cursor merely because the
            # same page still matches after a deny, expiry or unknown outcome.
            status = str(result.get("status") or "")
            performed.append({"sequence": step.sequence, "kind": step.kind, "effect_id": result.get("effect_id", "")})
            if run_journal is not None:
                run_journal.append(run.run_id, "stopped", position=cursor.position, reason=f"submit_{status}")
            return {"status": "stopped", "reason": f"submit_{status}", "at_step": step.sequence,
                    "effect_id": result.get("effect_id", ""), "performed": performed, "run_id": run.run_id}
        after = str(getattr(runtime, "last_snapshot_digest", "") or "")
        performed.append({"sequence": step.sequence, "kind": step.kind})
        if not cursor.confirm(step, after):
            if run_journal is not None:
                run_journal.append(run.run_id, "stopped", position=cursor.position, reason="unexpected_result")
            return {"status": "stopped", "reason": "unexpected_result", "at_step": step.sequence,
                    "detail": f"the page after step {step.sequence} is not what the candidate expects", "performed": performed, "run_id": run.run_id}
        if run_journal is not None:
            run_journal.append(run.run_id, "step_confirmed", step=step.sequence, position=cursor.position)
    if run_journal is not None:
        run_journal.append(run.run_id, "complete", position=cursor.position)
    return {"status": "complete", "steps": len(run.candidate.steps), "performed": performed, "run_id": run.run_id}
