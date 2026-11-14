"""Open or resume the Harness session behind a master conversation.

Kept out of ``ws_handler`` so that canonical source stays within its bounded
size; the binding rules are the same ones the console already had (registry
aliveness, provider fingerprint, and one allowance bound at creation).

A resume that is busy or unsettled keeps that binding. It is not a reason
to open a substitute session.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger


class MasterHarnessResumeBlocked(Exception):
    """Resume refused, and a new session must not stand in for this one."""

    def __init__(self, reason: str, harness_session_id: str) -> None:
        super().__init__(reason)
        self.reason = str(reason or "")
        self.harness_session_id = str(harness_session_id or "")


class MasterTurnFlight:
    """One console turn that other messages may steer into, not replace."""

    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.finished = asyncio.Event()
        self.session_id = ""
        self.turn_id = ""
        self.provider: Any = None
        self.blocked_reason = ""


def _flight_lock(handler: Any) -> asyncio.Lock:
    lock = getattr(handler, "_master_flight_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(handler, "_master_flight_lock", lock)
    return lock


def _flights(handler: Any) -> dict[str, MasterTurnFlight]:
    flights = getattr(handler, "_master_turn_flights", None)
    if flights is None:
        flights = {}
        setattr(handler, "_master_turn_flights", flights)
    return flights


async def claim_master_flight(handler: Any, key: str) -> tuple[bool, MasterTurnFlight]:
    """Own the console/task flight, or observe the one already running."""
    async with _flight_lock(handler):
        flights = _flights(handler)
        current = flights.get(key)
        if current is not None and not current.finished.is_set():
            return False, current
        flight = MasterTurnFlight()
        flights[key] = flight
        return True, flight


async def release_master_flight(handler: Any, key: str, flight: MasterTurnFlight) -> None:
    flight.finished.set()
    flight.ready.set()
    async with _flight_lock(handler):
        flights = _flights(handler)
        if flights.get(key) is flight:
            flights.pop(key, None)


def session_is_live(engine: Any, session_id: str) -> bool:
    checker = getattr(engine, "harness_turn_is_live", None)
    if callable(checker):
        try:
            return bool(checker(session_id))
        except Exception:
            return False
    live = getattr(engine, "__dict__", {}).get("_harness_live_turns", set())
    return str(session_id) in set(live or ())


async def steer_open_flight(flight: MasterTurnFlight, text: str) -> bool:
    """Queue text on the turn this flight already holds. Never opens a session."""
    if flight.finished.is_set():
        return False
    provider = flight.provider
    turn_id = str(flight.turn_id or "")
    session_id = str(flight.session_id or "")
    steer = getattr(provider, "steer_turn", None)
    if provider is None or not turn_id or not session_id or not callable(steer):
        return False
    result = await steer(session_id, text, expected_turn_id=turn_id)
    from ampuly.layer3_agent.harness import HARNESS_OUTCOME_UNKNOWN

    if getattr(result, "reason", "") == HARNESS_OUTCOME_UNKNOWN:
        from ampuly.plugins.office_ui.master_runtime import MasterSafetyStop

        raise MasterSafetyStop("Steer delivery is unknown; check the open turn before resending")
    return bool(getattr(result, "ok", False))


async def steer_live_master_turn(engine: Any, session_id: str, text: str) -> bool:
    """Steer the provider this process already registered for the session."""
    provider = engine.__dict__.get("_harness_live_providers", {}).get(str(session_id))
    turn_id = ""
    if provider is not None:
        turn_id = str(getattr(provider, "_running_turn_ids", {}).get(str(session_id), "") or "")
    flight = MasterTurnFlight()
    flight.provider = provider
    flight.session_id = str(session_id or "")
    flight.turn_id = turn_id
    return await steer_open_flight(flight, text)


async def open_master_harness_session(
    handler: Any,
    engine: Any,
    provider: Any,
    scope: Any,
    task: Any,
    console_session_id: str,
    budget_id: str = "",
) -> str:
    """Open or resume the Harness session this conversation is bound to.

    The binding on the conversation's record is a pointer, not an authority: the
    registry decides whether the session is still there, the provider fingerprint
    decides whether this executor opened it, and the allowance must be the one
    this conversation now spends from. A closed, missing, or budget-mismatched
    idle session may be replaced. A busy or unsettled one is kept.
    """
    from ampuly.application.execution_binding import ExecutionBinding, binding_of

    store = getattr(engine, "store", None)
    if store is None or not console_session_id:
        raise MasterHarnessResumeBlocked("store_unavailable", "")
    try:
        record = await store.get_session(console_session_id)
    except Exception as exc:
        logger.opt(exception=True).warning("master control: the conversation row could not be read")
        # A failed read says nothing about the old turn's terminal state.
        # Only a successful read may authorize replacing its binding.
        raise MasterHarnessResumeBlocked("store_unavailable", "") from exc
    existing = binding_of(record) if record is not None else None

    if existing is not None and existing.belongs_to(getattr(provider, "provider_name", "")):
        candidate = existing.harness_session_id
        alive = False
        try:
            alive = provider.registry.session(candidate) is not None
        except Exception as exc:
            logger.opt(exception=True).warning("master control: the harness registry could not be read")
            raise MasterHarnessResumeBlocked("registry_unavailable", candidate) from exc
        if alive:
            status_of = getattr(provider, "resume_status", None)
            if callable(status_of):
                reason = str(await status_of(candidate) or "")
            else:
                reason = "resumed" if await provider.resume_session(candidate) else "unsettled"
            if reason == "unsettled" and session_is_live(engine, candidate):
                reason = "busy"
            if reason == "resumed":
                # The allowance binds once, at session creation. A session opened
                # before this conversation had one cannot be re-bound later, so a
                # fresh session is opened rather than spending outside the budget.
                budget_of = getattr(provider, "budget_id_for_session", None)
                bound = str(budget_of(candidate) or "") if callable(budget_of) else ""
                if bound == str(budget_id or ""):
                    return candidate
                reason = "budget_mismatch"
            if reason in {"busy", "unsettled"}:
                raise MasterHarnessResumeBlocked(reason, candidate)

    context = {"task_id": str(getattr(task, "id", "") or "")}
    if budget_id:
        context["budget_id"] = str(budget_id)
    session_id = await provider.create_session(scope, context=context)
    await handler._master_bind_execution(
        engine,
        console_session_id,
        ExecutionBinding(
            console_session_id=console_session_id,
            harness_session_id=session_id,
            provider=str(getattr(provider, "provider_name", "") or ""),
            task_id=str(getattr(task, "id", "") or ""),
            bound_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        ),
    )
    return session_id
