"""Persist an asynchronous Builder attempt around the existing runner.

This is a lifecycle adapter, not a workflow scheduler. SQLite writes finish
before cancellation is acknowledged; no background writer can later turn a
cancelled claim into a new running attempt. A failed settlement stays visible
as running/unknown and is never replayed here.
"""
from __future__ import annotations

import asyncio
import copy
import math
from typing import Any, Callable

from ampuly.plugins.office_ui.builder_loop import BuilderError, BuilderSession, TrialResult
from ampuly.plugins.office_ui.process_spec import ProcessSpec

# Keep an unconfirmed coroutine observable and refuse further trial dispatch
# in this process. Completion removes only the local reference, never the
# durable unknown claim. No background retry or restart is scheduled.
_UNCONFIRMED_RUNNERS: set[asyncio.Task] = set()


def _observe_unconfirmed(execution: asyncio.Task) -> None:
    _UNCONFIRMED_RUNNERS.discard(execution)
    if not execution.cancelled():
        execution.exception()


async def _stop_runner(execution: asyncio.Task, *, cleanup_seconds: float) -> bool:
    """Request cancellation once; wait at most cleanup_seconds, even if recancelled.

    Python cannot forcibly kill an uncooperative coroutine. On deadline the
    caller must keep the durable claim unknown and refuse a replacement.
    """
    if not execution.done():
        execution.cancel()
    deadline = asyncio.get_running_loop().time() + cleanup_seconds
    while not execution.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            _UNCONFIRMED_RUNNERS.add(execution)
            execution.add_done_callback(_observe_unconfirmed)
            return False
        try:
            await asyncio.wait({execution}, timeout=remaining)
        except asyncio.CancelledError:
            # asyncio.wait does not send a second cancel into runner cleanup.
            continue
    if not execution.cancelled():
        execution.exception()
    return True


async def _critical_db(method: Callable, *args: Any, **kwargs: Any) -> tuple[Any, bool]:
    """One bounded store operation; defer cancellation until its thread exits."""
    worker = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    return worker.result(), cancelled


OFFLINE_TIMEOUT_CEILING = 120.0
# A trial that admitted a paid outside call (a node trial that generates images). One such call can take minutes on
# the provider's side, and cancelling it half way leaves a billed image nobody saved -- so the round is long enough
# to contain the calls it lets start. Nothing else gets this ceiling.
PAID_TIMEOUT_CEILING = 600.0


def _validate_limits(timeout_seconds: float, cleanup_seconds: float, *, paid: bool = False) -> None:
    ceiling = PAID_TIMEOUT_CEILING if paid is True else OFFLINE_TIMEOUT_CEILING
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= ceiling):
        raise ValueError(f"offline trial timeout must be finite and between 0 and {int(ceiling)} seconds")
    if (isinstance(cleanup_seconds, bool) or not isinstance(cleanup_seconds, (int, float))
            or not math.isfinite(cleanup_seconds) or not 0 < cleanup_seconds <= 5):
        raise ValueError("offline cleanup timeout must be finite and between 0 and 5 seconds")
    if _UNCONFIRMED_RUNNERS:
        raise BuilderError("trial_cleanup_unconfirmed: no further trial dispatch until the runner stops")


async def claim_saved_trial(
    business: Any, spec: ProcessSpec, *, actor: str,
    timeout_seconds: float = 120.0, cleanup_seconds: float = 5.0,
) -> tuple[ProcessSpec, dict[str, Any]]:
    """Durably claim one saved revision before a caller dispatches background work."""
    _validate_limits(timeout_seconds, cleanup_seconds)
    frozen = copy.deepcopy(spec)
    claim, cancelled = await _critical_db(
        business.reserve_builder_round, frozen.spec_id, frozen.digest(), actor=actor,
    )
    if cancelled:
        result = TrialResult(1, frozen.digest(), "unrun", reason="试跑启动前已取消",
                             detail="cancelled_after_claim; runner_not_started")
        payload = result.to_payload()
        payload["round"] = claim["round"]
        await _critical_db(business.finish_builder_round, claim["id"], payload, actor=actor, proposal={})
        raise asyncio.CancelledError
    return frozen, claim


async def run_claimed_trial(
    business: Any, spec: ProcessSpec, claim: dict[str, Any], *, actor: str,
    runner_factory: Callable, timeout_seconds: float = 120.0,
    cleanup_seconds: float = 5.0, paid: bool = False,
) -> dict[str, Any]:
    """Run a claim already made durable; used by synchronous and background callers."""
    _validate_limits(timeout_seconds, cleanup_seconds, paid=paid)
    frozen = copy.deepcopy(spec)
    session = BuilderSession(frozen)

    def invoke(workflow: dict):
        return runner_factory()(workflow)

    execution = asyncio.create_task(session.run_trial_async(invoke))
    cancelled = False
    try:
        result = await asyncio.wait_for(asyncio.shield(execution), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        if not await _stop_runner(execution, cleanup_seconds=cleanup_seconds):
            raise BuilderError("trial_cleanup_unconfirmed: durable claim retained; do not replay")
        result = TrialResult(1, frozen.digest(), "unrun", reason="试跑超过时限，结果未核验",
                             detail="trial_deadline_exceeded; no automatic retry or rollback assumption")
    except asyncio.CancelledError:
        if not await _stop_runner(execution, cleanup_seconds=cleanup_seconds):
            raise BuilderError("trial_cleanup_unconfirmed: durable claim retained; do not replay")
        cancelled = True
        result = TrialResult(1, frozen.digest(), "unrun", reason="试跑已中断，结果未核验",
                             detail="trial_cancelled; no pass evidence or rollback assumption")
    payload = result.to_payload()
    payload["round"] = claim["round"]
    proposal = session.proposals[0].to_payload() if session.proposals else {}
    if proposal:
        proposal["round"] = claim["round"]
    _, cancelled_settling = await _critical_db(
        business.finish_builder_round, claim["id"], payload, actor=actor, proposal=proposal,
    )
    if cancelled or cancelled_settling:
        raise asyncio.CancelledError
    return {"trial": payload, "proposal": proposal}


async def run_saved_trial(business: Any, spec: ProcessSpec, *, actor: str,
                          runner_factory: Callable, timeout_seconds: float = 120.0,
                          cleanup_seconds: float = 5.0) -> dict[str, Any]:
    """Claim, run and settle once. The owner check remains at the WS boundary.

    Even runner construction happens after admission. Duplicate requests
    cannot start a second runner, and a cancelled reservation never launches.
    The caller supplies a trusted server-side runner, never client code.
    """
    frozen, claim = await claim_saved_trial(
        business, spec, actor=actor,
        timeout_seconds=timeout_seconds, cleanup_seconds=cleanup_seconds,
    )
    return await run_claimed_trial(
        business, frozen, claim, actor=actor, runner_factory=runner_factory,
        timeout_seconds=timeout_seconds, cleanup_seconds=cleanup_seconds,
    )
