"""The native runtime, behind the same Harness contract as everyone else.

Until now the unified Harness was a boundary external Providers could be
reached through, while our own runtime was reached directly. That asymmetry
is the reason "runs go through the unified boundary" was never true: the
common path was the one path that did not use it.

This wraps ``NativeAgent`` -- the existing executor, not a second one -- so a
native run creates a session, records its events in the same registry a Codex
session would, and can be interrupted, resumed and asked for its artifacts
through the same calls. Nothing about the execution changes; what changes is
that it is now observable and addressable like every other Provider.

Kernel-adjacent but additive: ``NativeAgent.execute`` is called as it always
was, and no existing caller is redirected here.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from ampuly.core.approval_decision import normalize_escalation_reply
from ampuly.core.models import Task, TaskResult, TaskStatus
from ampuly.core.step_snapshot import bind_native_harness_turn
from ampuly.layer3_agent.harness import (
    HARNESS_INVALID,
    HARNESS_NOT_FOUND,
    HARNESS_NO_WAITER,
    HARNESS_OUT_OF_SCOPE,
    HARNESS_OUTCOME_UNKNOWN,
    HARNESS_REFUSED,
    HARNESS_SESSION_NOT_LIVE,
    HarnessCallbacks,
    HarnessEvent,
    HarnessOperationResult,
    HarnessProvider,
    HarnessScope,
    _invoke_callback,
    unsupported_operation,
)
from ampuly.layer3_agent.harness_sessions import HarnessSessionBusy, HarnessSessionLost, HarnessSessionRegistry
from ampuly.llm.budget import OWNER_INTERRUPT

PROVIDER_NAME = "native"

# The busy wait a terminal record gets. The claim it settles is what refuses
# automatic replay, so a settle that gives up after the ordinary second
# leaves a session refusing everything; ten seconds is what the registry
# allowed before the claim existed.
SETTLE_LOCK_TIMEOUT_SEC = 10.0


class HarnessSettleFailed(RuntimeError):
    """The turn ended, and the registry would not take the record of it."""


class _TurnSteerInbox(asyncio.Queue):
    """Seal a root turn's inbox before its asynchronous terminal cleanup."""

    def __init__(self) -> None:
        super().__init__(maxsize=32)
        self.accepting = True

    def close_if_empty(self) -> bool:
        # No await between this check and closing: an acknowledged steer is
        # either available to the next iteration or refused at enqueue time.
        if not self.empty():
            return False
        self.accepting = False
        return True

    def put_nowait(self, item: Any) -> None:
        if not self.accepting:
            raise RuntimeError("the turn inbox is closed")
        super().put_nowait(item)

    def close_and_take_pending(self) -> list[Any]:
        self.accepting = False
        pending = []
        while not self.empty():
            pending.append(self.get_nowait())
        return pending


# The approvals this channel is for. Approvals stay four separate kinds; a
# business gate, a delivery acceptance or a staffing choice carries more than
# a boolean and is answered by the domain command that owns it.
_HARNESS_APPROVAL_CHECKPOINTS: frozenset[str] = frozenset({"tool_permission", "action_permission"})

# Tokens that mean yes. Ordered narrowest-first where they are chosen.
_APPROVING_TOKENS: frozenset[str] = frozenset(
    {"approve_once", "approve_session", "always_project", "always_global"}
)


class NativeHarnessProvider(HarnessProvider):
    """One native turn, with the session and its events written down."""

    provider_name = PROVIDER_NAME

    def __init__(
        self,
        agent: Any,
        registry: HarnessSessionRegistry,
        *,
        interrupt_poll_sec: float = 2.0,
        interactions: Any = None,
    ) -> None:
        self.agent = agent
        self.registry = registry
        # The coordinator that owns durable owner decisions. Optional because
        # a bare provider (a trial, a test) has no owner to ask; without it
        # `resolve_approval` answers `unsupported` rather than pretending.
        self.interactions = interactions
        # How often a running turn looks for an interrupt request written by
        # another process. Progress events check too; this is for the turns
        # that report none.
        self.interrupt_poll_sec = float(interrupt_poll_sec)
        # A caller that already has a Task -- the console launching an order,
        # say -- hands it over rather than letting this rebuild one. The task
        # carries the workflow contract, the customer and the budget the run
        # was launched with, and a boundary that replaced them would be a hole
        # rather than a bridge.
        self._adopted: dict[str, Task] = {}
        self._scopes: dict[str, HarnessScope] = {}
        self._budgets: dict[str, str] = {}
        self._interrupted: set[str] = set()
        self._running_turns: dict[str, asyncio.Task[TaskResult]] = {}
        self._running_turn_ids: dict[str, str] = {}
        self._steer_inboxes: dict[str, tuple[str, asyncio.Queue[Any]]] = {}


    @staticmethod
    async def _drain(worker: asyncio.Task[Any]) -> bool:
        """Wait for a worker thread to finish; say whether a cancel landed meanwhile."""
        cancelled = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        return cancelled

    async def _db(self, method: Any, *args: Any, **kwargs: Any) -> Any:
        """Move bounded SQLite waits off the event loop; drain on cancellation.

        Cancelling to_thread cannot stop its worker. Wait for that operation
        before settling cancellation, so it cannot commit after settlement.
        Each registry method owns its connection entirely on the worker.
        """
        worker = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
        if await self._drain(worker):
            if not worker.cancelled():
                worker.exception()
            raise asyncio.CancelledError
        return worker.result()

    async def _settle(self, session_id: str, turn_id: str, *, event_type: str, data: dict[str, Any],
                      state_after: str, state_reason: str, failing: Exception | None = None) -> None:
        """Write the terminal record, and let little short of a missing database stop it.

        The outcome is known here -- the turn finished or failed in this
        process -- so the write gets the long busy wait a terminal fact
        deserves, and a cancellation landing meanwhile is drained rather than
        obeyed: the record is authoritative, and a cancel that arrives after
        the turn ended changes nothing about what happened. When the write
        still fails, the claim stays pending on purpose (replay is refused
        until a person settles it with ``settle_pending_turn``), the failure
        is logged, and the caller's own exception keeps precedence.
        """
        worker = asyncio.create_task(asyncio.to_thread(
            self.registry.record_event, session_id, event_type=event_type, turn_id=turn_id, data=data,
            state_after=state_after, state_reason=state_reason, settle_turn=True,
            lock_timeout=SETTLE_LOCK_TIMEOUT_SEC,
        ))
        await self._drain(worker)
        try:
            worker.result()
        except Exception as settle_exc:
            note = (f"terminal event {event_type} for {turn_id} on {session_id} could not be written "
                    f"({type(settle_exc).__name__}: {settle_exc}); the turn's claim stays pending until "
                    "a person settles it")
            logger.error(note)
            if failing is not None:
                failing.add_note(note)
                return
            raise HarnessSettleFailed(note) from settle_exc

    # ── sessions ──

    async def create_session(
        self,
        scope: HarnessScope,
        profile: str = "",
        context: dict[str, Any] | None = None,
    ) -> str:
        scope.validate()
        budget_id = str((context or {}).get("budget_id", "") or "")
        record = await self._db(self.registry.open_session,
            provider=self.provider_name,
            project_id=scope.project_id,
            customer_id=scope.customer_id,
            environment_id=scope.environment_id,
            run_id=scope.run_id,
            order_id=str((context or {}).get("order_id", "") or ""),
            task_id=str((context or {}).get("task_id", "") or ""),
            initial_context={"profile": profile, "workspace": scope.workspace_path, "budget_id": budget_id},
        )
        self._scopes[record.session_id] = scope
        if budget_id:
            self._budgets[record.session_id] = budget_id
        return record.session_id

    async def resume_status(self, session_id: str) -> str:
        """Why a resume can or cannot continue this session.

        ``resumed`` is the only reason a caller may spend on this session.
        ``busy`` means a turn is still running here. ``unsettled`` means a
        turn was started and has no terminal record — including after this
        process restarted. Neither is permission to open a substitute.
        """
        session_id = str(session_id or "")
        if session_id in self._running_turns:
            return "busy"
        record = await self._db(self.registry.session, session_id)
        if record is None:
            return "missing"
        if record.state == "closed":
            return "closed"
        if record.state == "lost":
            return "lost"
        if record.provider != self.provider_name:
            return "provider_mismatch"
        if await self._db(self.registry.pending_turn_id, session_id):
            return "unsettled"
        if await self.resume_session(session_id):
            return "resumed"
        if session_id in self._running_turns:
            return "busy"
        if await self._db(self.registry.pending_turn_id, session_id):
            return "unsettled"
        record = await self._db(self.registry.session, session_id)
        if record is None:
            return "missing"
        if getattr(record, "state", "") == "closed":
            return "closed"
        if getattr(record, "state", "") == "lost":
            return "lost"
        return "unsettled"

    async def resume_session(self, session_id: str) -> bool:
        if session_id in self._running_turns:
            return False
        record = await self._db(self.registry.session, session_id)
        if record is None or record.state in {"closed", "lost"} or record.provider != self.provider_name:
            return False
        if await self._db(self.registry.pending_turn_id, session_id):
            return False
        await self._scope_for(session_id, record)
        try:
            await self._db(self.registry.set_state, session_id, "active", reason="resumed", require_idle=True)
        except (HarnessSessionLost, HarnessSessionBusy):
            return False
        self._interrupted.discard(session_id)
        await self._db(self.registry.record_event, session_id, event_type="session_resumed")
        return True

    async def close_session(self, session_id: str) -> bool:
        if session_id in self._running_turns:
            raise ValueError("interrupt and await the running turn before closing its session")
        await self._db(self.registry.set_state, session_id, "closed", reason="closed by caller", require_idle=True)
        self._scopes.pop(session_id, None)
        self._adopted.pop(session_id, None)
        self._budgets.pop(session_id, None)
        self._interrupted.discard(session_id)
        return True

    async def interrupt(self, session_id: str) -> bool:
        """Request cancellation of our owned coroutine; settle only after cleanup.

        NativeRuntimeV2 retains its existing cancellation settlement. A pending
        external effect can remain unknown; interrupt is not a rollback claim.
        """
        record = await self._db(self.registry.session, session_id)
        if record is None or record.state != "active":
            return False
        running = self._running_turns.get(session_id)
        if running is not None:
            self._interrupted.add(session_id)
            await self._db(self.registry.record_event, session_id, event_type="interrupt_requested")
            running.cancel(OWNER_INTERRUPT)
        else:
            try:
                await self._db(self.registry.set_state, session_id, "suspended", reason="interrupted before another turn", require_idle=True)
            except HarnessSessionBusy:
                # A different bridge/process owns that turn; do not pretend to
                # cancel it by merely changing the persisted session label.
                return False
            await self._db(self.registry.record_event, session_id, event_type="interrupt_requested")
        return True

    async def request_interrupt(self, session_id: str, *, actor: str = "owner") -> str:
        """Stop a turn wherever it runs: here directly, elsewhere through the registry.

        Returns the turn id the request was recorded against, or '' when no
        turn is pending. A turn in another process honours the request at
        its next progress event; until its own terminal record lands, the
        outcome is still that process's to report.
        """
        if session_id in self._running_turns:
            turn_id = await self._db(self.registry.pending_turn_id, session_id)
            await self.interrupt(session_id)
            return turn_id
        return await self._db(self.registry.request_interrupt, session_id, actor=actor)

    async def settle_pending_turn(self, session_id: str, *, reason: str, actor: str = "owner") -> str:
        """A person closes a turn whose terminal record never landed; never automatic."""
        if session_id in self._running_turns:
            raise ValueError("that turn is still running in this process; interrupt it instead")
        return await self._db(self.registry.settle_pending_turn, session_id, reason=reason, actor=actor)

    # ── one turn ──

    async def start_turn(
        self, session_id: str, instruction: str, inputs: dict[str, Any] | None = None,
        callbacks: HarnessCallbacks | None = None,
    ) -> TaskResult:
        if session_id in self._running_turns:
            raise ValueError("a turn is already running for this session")
        turn_id = f"turn-{uuid.uuid4().hex[:8]}"
        before = await self._db(self.registry.session, session_id)
        since_seq = int(getattr(before, "last_seq", 0) or 0)
        running = asyncio.create_task(self._execute_turn(session_id, instruction, inputs, callbacks, turn_id=turn_id))
        self._running_turns[session_id] = running
        self._running_turn_ids[session_id] = turn_id
        # The same loop that listens for an interrupt also says this
        # process is still here. One task, one cadence, and the beat
        # stops exactly when the watcher does.
        watcher = asyncio.create_task(self._watch_interrupts(session_id, running, since_seq, turn_id=turn_id))
        try:
            return await running
        except asyncio.CancelledError:
            if running.done() and not running.cancelled() and running.exception() is None:
                # The turn ended and its record says so; a cancel that landed
                # on the terminal write changes nothing about what happened.
                return running.result()
            record = await self._db(self.registry.session, session_id)
            if record is not None and record.state not in {"closed", "lost"}:
                try:
                    await self._db(self.registry.record_event, session_id, event_type="turn_cancelled", turn_id=turn_id,
                                  data={"rollback_claimed": False}, state_after="suspended", settle_turn=True,
                                  state_reason="turn cancelled; effect outcomes require their own verification")
                except HarnessSessionBusy:
                    # Cancellation may precede acquisition, or follow a commit.
                    # It must never suspend a competing bridge's owned turn.
                    pass
            if session_id in self._interrupted:
                return TaskResult(status=TaskStatus.CANCELLED, content="Interrupted; in-flight effects were not assumed rolled back.")
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            self._running_turns.pop(session_id, None)
            self._running_turn_ids.pop(session_id, None)
            self._interrupted.discard(session_id)

    async def _watch_interrupts(
        self, session_id: str, running: asyncio.Task[Any], since_seq: int, *, turn_id: str = "",
    ) -> None:
        """Poll for an interrupt written elsewhere, and say this process is still here.

        A turn with no progress hears the interrupt here. The heartbeat
        rides the same loop because a second task would be a second
        thing to leak, and a beat that outlived the watcher would claim
        a turn nobody is watching.
        """
        await self._beat(session_id, turn_id)
        while not running.done():
            await asyncio.sleep(self.interrupt_poll_sec)
            if running.done() or session_id in self._interrupted:
                return
            await self._beat(session_id, turn_id)
            try:
                requested = await self._db(self.registry.interrupt_requested_after, session_id, since_seq, turn_id=turn_id)
            except Exception:
                # A busy registry is not a reason to stop watching; the next
                # tick asks again.
                continue
            if requested:
                self._interrupted.add(session_id)
                running.cancel(OWNER_INTERRUPT)
                return

    async def _beat(self, session_id: str, turn_id: str) -> None:
        """Write the lease, or let it lapse.

        A registry that will not take the beat is not a reason to stop
        the turn: the lease going stale reads as "probably gone", which
        is a safer wrong answer than an exception here would be.
        """
        if not turn_id:
            return
        try:
            await self._db(
                self.registry.beat_turn, session_id,
                turn_id=turn_id, period_sec=self.interrupt_poll_sec,
            )
        except Exception:
            return

    async def _execute_turn(
        self,
        session_id: str,
        instruction: str,
        inputs: dict[str, Any] | None = None,
        callbacks: HarnessCallbacks | None = None,
        *, turn_id: str,
    ) -> TaskResult:
        record = await self._db(self.registry.session, session_id)
        if record is None:
            raise ValueError(f"unknown session {session_id!r}")
        if record.state == "lost":
            raise HarnessSessionLost(f"session {session_id!r} was lost; start a new one")
        if record.state != "active":
            raise ValueError(f"session {session_id!r} is {record.state}")

        if record.provider != self.provider_name:
            raise ValueError("session belongs to another provider")
        scope = await self._scope_for(session_id, record)
        task = self._task_for(session_id, record, scope, instruction, inputs or {})

        started = await self._db(self.registry.record_event, session_id, event_type="turn_started", turn_id=turn_id,
                                 data={"task_id": task.id}, exclusive_turn=True)

        async def on_progress(text: str) -> None:
            await self._db(self.registry.record_event, session_id, event_type="progress", turn_id=turn_id, data={"text": text})
            await _invoke_callback(getattr(callbacks, "on_progress", None), text)
            # Somebody in another process may have asked for this turn to
            # stop. Progress is the one moment the turn is listening, so
            # this is where the request is honoured -- the same way an
            # in-process interrupt lands, through the running task.
            if session_id not in self._interrupted and await self._db(
                self.registry.interrupt_requested_after, session_id, int(getattr(started, "seq", 0) or 0), turn_id=turn_id
            ):
                self._interrupted.add(session_id)
                running = self._running_turns.get(session_id)
                if running is not None:
                    running.cancel(OWNER_INTERRUPT)

        # Root turns have no parent inbox. Reuse NativeAgent's existing inbox
        # consumer with a bounded, turn-owned queue; never hijack a parent's
        # channel or carry an unconsumed correction into the following turn.
        owned_inbox = None
        if getattr(task, "_runtime_inbox_queue", None) is None:
            owned_inbox = _TurnSteerInbox()
            setattr(task, "_runtime_inbox_queue", owned_inbox)
            self._steer_inboxes[session_id] = (turn_id, owned_inbox)
        try:
            await _invoke_callback(
                getattr(callbacks, "on_event", None),
                HarnessEvent(event_type="turn_started", session_id=session_id, turn_id=turn_id, data={"task_id": task.id}),
            )
            # Only the Provider that durably claimed this turn may bind it.
            # ContextVars reset on success, exception and cancellation.
            with bind_native_harness_turn(
                task, session_id=session_id, turn_id=turn_id, run_id=record.run_id,
                project_id=record.project_id, customer_id=record.customer_id,
                environment_id=record.environment_id,
            ):
                result = await self.agent.execute(task, on_progress=on_progress)
                from ampuly.layer3_agent.employee_runtime import current_employee_turn
                employee = current_employee_turn(task)
                if employee is not None:
                    # Managed employee executors also use this Provider without
                    # passing through NativeAgent.execute. Persist their outputs
                    # before settling DONE in the shared Harness registry.
                    await employee.finalize_result(result)
        except Exception as exc:
            # A failed turn is a fact about the run, not a gap in its record.
            await self._settle(session_id, turn_id, event_type="turn_failed",
                data={"error_type": type(exc).__name__, "error": str(exc)[:500]},
                state_after="suspended", state_reason=f"turn failed: {type(exc).__name__}", failing=exc,
            )
            raise

        finally:
            self._steer_inboxes.pop(session_id, None)
            if owned_inbox is not None and getattr(task, "_runtime_inbox_queue", None) is owned_inbox:
                setattr(task, "_runtime_inbox_queue", None)

        status = getattr(result, "status", None)
        artifacts = dict(getattr(result, "artifacts", {}) or {})
        pause_reason = ("interrupted by a person" if session_id in self._interrupted else
                        "waiting for a person" if status == TaskStatus.AWAITING_HUMAN else "")
        await self._settle(session_id, turn_id, event_type="turn_finished",
            data={"status": getattr(status, "value", str(status)), "chars": len(str(getattr(result, "content", "") or "")),
                  "artifacts": artifacts},
            state_after="suspended" if pause_reason else "", state_reason=pause_reason,
        )
        return result

    async def collect_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        record = await self._db(self.registry.session, session_id)
        if record is None or record.provider != self.provider_name:
            raise ValueError("unknown native session")
        # References share the authoritative completion event, not another
        # table or an in-memory cache. They remain readable after close/restart.
        events = await self._db(self.registry.events_after, session_id, limit=1001, event_type="turn_finished")
        if len(events) > 1000:
            raise ValueError("artifact history exceeds one page; use the paginated session event stream")
        return [{**event.data["artifacts"], "turn_id": event.turn_id}
                for event in events if isinstance(event.data.get("artifacts"), dict) and event.data["artifacts"]]

    # ── helpers ──

    async def _scope_for(self, session_id: str, record: Any) -> HarnessScope:
        existing = self._scopes.get(session_id)
        if existing is not None:
            return existing
        first = await self._db(self.registry.events_after, session_id, 0, limit=1)
        if not first or first[0].event_type != "session_created" or not str(first[0].data.get("workspace", "")).strip():
            raise HarnessSessionLost("durable workspace scope is missing; do not resume into a default directory")
        scope = HarnessScope(project_id=record.project_id, workspace_path=first[0].data["workspace"],
                             customer_id=record.customer_id, environment_id=record.environment_id, run_id=record.run_id)
        scope.validate()
        self._scopes[session_id] = scope
        budget = str(first[0].data.get("budget_id", "") or "")
        if budget:
            self._budgets[session_id] = budget
        return scope

    def adopt_task(self, session_id: str, task: Task) -> None:
        """Run this exact task on this session's next turn."""
        self._adopted[str(session_id)] = task

    async def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        approved: bool,
        decision: dict[str, Any] | None = None,
    ) -> HarnessOperationResult:
        """Deliver one owner decision to the approval a turn is waiting on.

        The durable checkpoint is the authority, and the coordinator that
        accepts it already wakes the live waiter -- so this adds no second
        way to answer an approval. What it adds is the check between an id
        that arrived over an API and that write: the checkpoint has to belong
        to this session, and it has to be the kind of approval this channel
        is for.
        """

        session_key = str(session_id or "").strip()
        coordinator = self.interactions
        if coordinator is None:
            return unsupported_operation(
                "approval",
                alternative="answer the durable checkpoint through the interaction coordinator",
            )
        record = await self._db(self.registry.session, session_key)
        if record is None:
            return await self._approval_refusal(
                session_key, approval_id, HARNESS_NOT_FOUND, "no such harness session"
            )
        if str(getattr(coordinator, "project_id", "") or "") != str(record.project_id or ""):
            return await self._approval_refusal(
                session_key, approval_id, HARNESS_OUT_OF_SCOPE,
                "the coordinator serves a different project",
            )
        checkpoint = await coordinator.store.get_execution_checkpoint(
            str(approval_id or ""), project_id=str(record.project_id or "")
        )
        if checkpoint is None:
            return await self._approval_refusal(
                session_key, approval_id, HARNESS_NOT_FOUND, "no such checkpoint in this project"
            )
        kind = str(checkpoint.checkpoint_type or "").strip()
        if kind not in _HARNESS_APPROVAL_CHECKPOINTS:
            return await self._approval_refusal(
                session_key, approval_id, HARNESS_INVALID,
                f"{kind or 'this checkpoint'} is not a tool or action permission",
                alternative="answer it through the domain command that owns that decision",
            )
        # An id that arrived over an API is not a claim of ownership. The
        # association is checked here and the scope comes from the record.
        if not self._checkpoint_belongs(checkpoint, session_key):
            return await self._approval_refusal(
                session_key, approval_id, HARNESS_OUT_OF_SCOPE,
                "the checkpoint belongs to another session",
            )
        answer, problem = self._approval_decision(checkpoint, bool(approved), decision)
        if problem:
            return await self._approval_refusal(
                session_key, approval_id, HARNESS_INVALID, problem
            )
        receipt = await coordinator.submit(
            checkpoint_id=str(approval_id or ""),
            checkpoint_type=kind,
            decision=answer,
            # Deterministic, so a retransmitted decision is a duplicate and
            # not a second answer racing the first.
            client_request_id=(
                f"harness:{session_key}:{approval_id}:"
                f"{coordinator.decision_hash(answer)}"
            ),
            checkpoint=checkpoint,
        )
        if not receipt.acknowledged:
            outcome = str(receipt.outcome or "")
            return await self._approval_refusal(
                session_key, approval_id,
                HARNESS_REFUSED if outcome == "conflict" else HARNESS_INVALID,
                outcome or "the coordinator did not accept the decision",
            )
        await self._db(
            self.registry.record_event, session_key,
            event_type="approval_resolved",
            data={
                "approval_id": str(approval_id or ""),
                "approved": bool(approved),
                "option_id": str(answer.get("option_id", "") or ""),
                "outcome": str(receipt.outcome or ""),
                "live_waiter_notified": bool(receipt.live_waiter_notified),
            },
        )
        if not receipt.live_waiter_notified:
            # Recorded, and nobody was blocked on it. Both are successes and
            # only one changed what a turn is doing; a caller told they are
            # the same will report a run as resumed that is not running.
            return HarnessOperationResult(
                ok=True,
                reason=HARNESS_NO_WAITER,
                detail="the decision is recorded; no live turn was waiting for it",
            )
        return HarnessOperationResult(ok=True)

    async def _approval_refusal(
        self,
        session_id: str,
        approval_id: str,
        reason: str,
        detail: str,
        *,
        alternative: str = "",
    ) -> HarnessOperationResult:
        """Write the refusal down and return it with its reason attached."""
        try:
            await self._db(
                self.registry.record_event, session_id,
                event_type="approval_resolution_failed",
                data={"approval_id": str(approval_id or ""), "reason": reason, "detail": detail},
            )
        except Exception:  # noqa: BLE001 - a refusal that cannot be written is still a refusal
            pass
        return HarnessOperationResult(
            ok=False, reason=reason, detail=detail, alternative=alternative
        )

    async def _steer_refused(
        self,
        session_id: str,
        message: str,
        reason: str,
        detail: str,
    ) -> HarnessOperationResult:
        """Write the refusal down and hand back the same thing recorded."""
        outcome = HarnessOperationResult(ok=False, reason=reason, detail=detail)
        try:
            await self._db(
                self.registry.record_event, session_id,
                event_type="steer_unsupported",
                data={"text": message[:200], **outcome.to_payload()},
            )
        except Exception:  # noqa: BLE001 - losing the note is not losing the refusal
            pass
        return outcome

    def _checkpoint_belongs(self, checkpoint: Any, session_id: str) -> bool:
        """Whether this session is the one that raised the approval.

        Empty is not a match. A checkpoint with no session recorded on it
        cannot be shown to belong here, and treating "unknown" as "mine" is
        how one session answers another's question.
        """
        owner = str(getattr(checkpoint, "session_id", "") or "").strip()
        if not owner:
            return False
        known = {session_id}
        adopted = self._adopted.get(session_id)
        if adopted is not None:
            known.add(str(getattr(adopted, "session_id", "") or "").strip())
            metadata = dict(getattr(adopted, "metadata", {}) or {})
            known.add(str(metadata.get("harness_session_id", "") or "").strip())
        known.discard("")
        return owner in known

    @staticmethod
    def _approval_decision(
        checkpoint: Any,
        approved: bool,
        decision: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], str]:
        """The decision payload to submit, or why there isn't one.

        A caller may name the exact option. If it does, it has to agree with
        the boolean it came with -- `approved=False` carrying an approving
        option is two decisions, and whichever one loses becomes an audit
        record that disagrees with what happened.

        Otherwise the option is chosen from what the checkpoint actually
        offers, narrowest approval first. A boolean that quietly resolved to
        "always, every project" would widen an authority nobody granted.
        """
        answer = dict(decision or {})
        offered = [
            str(option.get("id", "") or "").strip()
            for option in list(
                dict((getattr(checkpoint, "payload", {}) or {}).get("interaction", {}) or {}).get(
                    "options", []
                )
                or []
            )
            if isinstance(option, dict) and str(option.get("id", "") or "").strip()
        ]
        if not offered:
            return {}, "the checkpoint offers no options to choose from"
        chosen = str(answer.get("option_id", "") or "").strip()
        if chosen:
            if chosen not in offered:
                return {}, "the named option is not one this checkpoint offers"
            if (normalize_escalation_reply(chosen) in _APPROVING_TOKENS) != approved:
                return {}, "the decision payload contradicts the approved flag"
        else:
            preference = (
                ("approve_once", "approve_session", "always_project", "always_global")
                if approved
                else ("deny",)
            )
            for token in preference:
                chosen = next(
                    (
                        option
                        for option in offered
                        if normalize_escalation_reply(option) == token
                    ),
                    "",
                )
                if chosen:
                    break
            if not chosen:
                return {}, "the checkpoint offers no option matching this decision"
        answer["option_id"] = chosen
        return answer, ""

    async def steer_turn(
        self, session_id: str, text: str, *, expected_turn_id: str,
    ) -> HarnessOperationResult:
        if not expected_turn_id:
            return HarnessOperationResult(ok=False, reason=HARNESS_INVALID, detail="expected_turn_id required")
        return await self.steer(session_id, text, expected_turn_id=expected_turn_id)

    async def steer(
        self, session_id: str, text: str, *, expected_turn_id: str = "",
    ) -> HarnessOperationResult:
        """Say something to a turn that is already running.

        The native loop already reads a queue between steps -- that is how a
        parent reaches a subagent mid-run. Steering uses the same queue rather
        than opening a second way in: a message that arrived by a different
        road would not be visible to anything that watches this one.

        Returns False when there is no running turn to reach. A steer that
        quietly went nowhere would leave somebody believing they had changed
        the run's direction.
        """
        message = str(text or "").strip()
        if not message:
            return HarnessOperationResult(
                ok=False, reason=HARNESS_INVALID, detail="an empty steer says nothing"
            )
        # An id nobody has heard of is a different fact from a session with
        # no turn running: one is a wrong id and no retry helps, the other
        # wants a turn started first. Checked before anything is recorded,
        # because the registry refuses an event for a session it does not
        # know and that refusal used to escape as an exception.
        if await self._db(self.registry.session, str(session_id or "")) is None:
            return HarnessOperationResult(
                ok=False, reason=HARNESS_NOT_FOUND, detail="no such harness session"
            )
        # Re-check AFTER the database await, immediately before enqueueing.
        # No await separates the identity check from the queue write.
        if expected_turn_id:
            running = self._running_turns.get(session_id)
            if (self._running_turn_ids.get(session_id) != expected_turn_id
                    or running is None or running.done()):
                return HarnessOperationResult(
                    ok=False, reason=HARNESS_SESSION_NOT_LIVE,
                    detail="the requested turn is no longer running here",
                )
        task = self._adopted.get(str(session_id))
        queue = getattr(task, "_runtime_inbox_queue", None) if task is not None else None
        if expected_turn_id:
            owned = self._steer_inboxes.get(session_id)
            queue = owned[1] if owned and owned[0] == expected_turn_id else None
        if queue is None or not hasattr(queue, "put_nowait") or not getattr(queue, "accepting", True):
            return await self._steer_refused(
                session_id, message, HARNESS_SESSION_NOT_LIVE,
                "no running turn with an inbox",
            )
        try:
            queue.put_nowait({"from": "owner", "body": message, "steered": True})
        except Exception as exc:  # noqa: BLE001 - a full queue is a real refusal
            return await self._steer_refused(
                session_id, message, HARNESS_REFUSED, type(exc).__name__
            )
        try:
            await self._db(
                self.registry.record_event, session_id, event_type="steered",
                turn_id=expected_turn_id, data={"chars": len(message), "delivery": "queued"},
            )
        except Exception:
            return HarnessOperationResult(
                ok=False, reason=HARNESS_OUTCOME_UNKNOWN,
                detail="instruction was queued but its receipt could not be saved; do not resend",
            )
        return HarnessOperationResult(ok=True, detail="queued for the turn; model consumption is not confirmed")

    def budget_id_for_session(self, session_id: str) -> str:
        """The allowance this session's turns spend from; '' when none was bound."""
        return str(self._budgets.get(str(session_id or ""), "") or "")

    def _task_for(
        self,
        session_id: str,
        record: Any,
        scope: HarnessScope | None,
        instruction: str,
        inputs: dict[str, Any],
    ) -> Task:
        """Build the Task the native executor expects, carrying the scope.

        The scope travels in metadata so the permission and budget layers see
        the same customer, environment and run the session was opened with --
        a Provider boundary that dropped them would be a hole, not a bridge.
        """
        adopted = self._adopted.get(str(session_id))
        additional = dict(inputs.get("metadata", {}) or {})
        if set(additional) - {"business_context", "request_id", "note"}:
            raise ValueError("turn metadata cannot define authority; bind it through the server-owned task and scope")
        bound_budget = self._budgets.get(session_id, "")
        if record.task_id and inputs.get("task_id") and str(inputs["task_id"]) != record.task_id:
            raise ValueError("turn task differs from the bound session")
        if inputs.get("budget_id") and str(inputs["budget_id"]) != bound_budget:
            raise ValueError("turn budget differs from the bound session")
        if adopted is not None:
            # Stamp the session onto it so the run is addressable, and change
            # nothing else about a task somebody else assembled.
            metadata = dict(adopted.metadata or {})
            if adopted.project_id != record.project_id or (record.task_id and adopted.id != record.task_id):
                raise ValueError("adopted task differs from the bound session")
            for key in ("customer_id", "environment_id", "order_id"):
                expected = str(getattr(record, key, "") or "")
                if str(metadata.get(key, "") or "") != expected:
                    raise ValueError(f"adopted task {key} differs from the bound session")
            if metadata.get("run_id") and str(metadata["run_id"]) != record.run_id:
                raise ValueError("adopted task run_id differs from the bound session")
            if scope is not None:
                if metadata.get("workspace_root") and Path(metadata["workspace_root"]).resolve() != Path(scope.workspace_path).resolve():
                    raise ValueError("adopted task workspace differs from the bound session")
                metadata["workspace_root"] = scope.workspace_path
            if bound_budget and metadata.get("budget_id") != bound_budget:
                raise ValueError("adopted task budget differs from the bound session")
            adopted.metadata = {**metadata, "harness_session_id": session_id}
            return adopted
        metadata = {
            "harness_session_id": session_id,
            "project_id": record.project_id,
            "customer_id": record.customer_id,
            "environment_id": record.environment_id,
            "run_id": record.run_id,
            "order_id": record.order_id,
        }
        # The budget travels with the scope: a paid tool reached through this
        # boundary must spend from the order's allowance, not from nowhere.
        budget_id = bound_budget
        if budget_id:
            metadata["budget_id"] = budget_id
        if scope is not None and scope.workspace_path:
            metadata["workspace_root"] = scope.workspace_path
        metadata.update(additional)
        return Task(
            id=str(inputs.get("task_id") or record.task_id or f"{session_id}:{uuid.uuid4().hex[:6]}"),
            title=str(inputs.get("title") or instruction[:60]),
            description=instruction,
            project_id=record.project_id,
            session_id=session_id,
            metadata=metadata,
        )
