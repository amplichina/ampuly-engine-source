"""Provider-neutral Harness contract for SuperOnes.

The business layer talks to this small surface instead of importing a
provider's process/session model.  ``AdapterHarnessProvider`` is deliberately
thin: it adapts the existing ExternalAgentAdapter without weakening the
runtime_v2 safety gates.  Providers can add richer event/control transports
later while keeping the same session and turn vocabulary.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib

from loguru import logger
import inspect
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ampuly.core.models import AgentStatus, Task, TaskResult, TaskStatus
from ampuly.layer3_agent.adapters.base import ExternalAgentAdapter
from ampuly.layer3_agent.workspace_artifacts import (
    MAX_RECORDED_CHANGES,
    baseline_record,
    changes_record,
    WorkspaceSnapshot,
    collect_turn_artifacts,
    snapshot_workspace,
)


@dataclass(frozen=True)
class HarnessScope:
    """The immutable scope a Provider is allowed to observe and mutate."""

    project_id: str
    workspace_path: str
    run_id: str = ""
    customer_id: str = ""
    environment_id: str = ""

    def validate(self) -> None:
        """Reject an unusable scope before a Provider process can start."""
        if not str(self.project_id or "").strip():
            raise ValueError("harness scope requires project_id")
        if not str(self.workspace_path or "").strip():
            raise ValueError("harness scope requires workspace_path")


@dataclass
class HarnessEvent:
    """Normalized event emitted by a Harness turn."""

    event_type: str
    session_id: str
    turn_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)


# Why an interactive operation did not happen. Stable tokens, because a
# caller branches on them and an event log is read months later.
HARNESS_UNSUPPORTED = "unsupported"          # this provider has no such channel
HARNESS_SESSION_NOT_LIVE = "session_not_live"  # nothing running to reach
HARNESS_NOT_FOUND = "not_found"              # no such approval / call
HARNESS_OUT_OF_SCOPE = "out_of_scope"        # it belongs to another session
HARNESS_INVALID = "invalid"                  # the payload is not answerable
HARNESS_REFUSED = "refused"                  # the far end considered it and said no
HARNESS_NO_WAITER = "no_waiter"              # recorded, but nobody was waiting
HARNESS_OUTCOME_UNKNOWN = "outcome_unknown"  # dispatched, acknowledgement unavailable

HARNESS_OPERATION_REASONS: frozenset[str] = frozenset({
    HARNESS_UNSUPPORTED,
    HARNESS_SESSION_NOT_LIVE,
    HARNESS_NOT_FOUND,
    HARNESS_OUT_OF_SCOPE,
    HARNESS_INVALID,
    HARNESS_REFUSED,
    HARNESS_NO_WAITER,
    HARNESS_OUTCOME_UNKNOWN,
})


@dataclass(frozen=True)
class HarnessOperationResult:
    """The outcome of one interactive operation, with its reason.

    ``ok`` is what the operation used to return on its own. ``reason`` is what
    was missing: a caller handed a bare ``False`` cannot tell a provider that
    has no control channel from a session that merely is not live, so it
    either retries forever or gives up on something that would have worked.

    ``alternative`` is the other half of the same rule -- an interface that
    cannot serve a request says what will. Empty when the answer is simply no.

    Truthy exactly when ``ok``, so existing boolean call sites keep working
    and can learn the reason whenever they are ready to.
    """

    ok: bool
    reason: str = ""
    detail: str = ""
    alternative: str = ""

    def __post_init__(self) -> None:
        if self.ok and self.reason and self.reason != HARNESS_NO_WAITER:
            raise ValueError("a successful operation states no failure reason")
        if self.reason and self.reason not in HARNESS_OPERATION_REASONS:
            raise ValueError(f"unknown harness operation reason: {self.reason}")

    def __bool__(self) -> bool:
        return bool(self.ok)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": bool(self.ok)}
        for key, value in (
            ("reason", self.reason),
            ("detail", self.detail),
            ("alternative", self.alternative),
        ):
            if value:
                payload[key] = value
        return payload


def unsupported_operation(operation: str, *, alternative: str) -> HarnessOperationResult:
    """The standing answer for a channel a provider does not have."""

    return HarnessOperationResult(
        ok=False,
        reason=HARNESS_UNSUPPORTED,
        detail=f"this provider has no {operation} channel",
        alternative=alternative,
    )


class HarnessCallbacks(Protocol):
    """Optional callbacks; implementations may accept only the hooks they need."""

    def on_event(self, event: HarnessEvent) -> Any: ...

    def on_progress(self, text: str) -> Any: ...


class HarnessControlTransport(Protocol):
    """Optional Provider-owned control channel for a live session.

    A seam, not a capability: nothing in ``ampuly/`` implements it and the engine never supplies one (checked
    2026-09-19; a test pins the engine's construction). It was looked at for the CLI adapters and deliberately left
    empty: the running process is a local of the broker's monitor loop, approvals are already answered inline there,
    and no CLI has a mid-turn steer message. Two things to know before attaching one:

    * it REPLACES the CLI cancel path -- ``interrupt`` and ``resume_session`` delegate to the transport and skip the
      process stop that the capability matrix's ``interrupt`` / ``cancel`` rows are evidence for, so a transport
      built for one verb has to answer all four honestly;
    * production steering does not reach it -- the engine calls ``steer_turn`` (bound to a turn), which the adapter
      provider does not override; the session-only ``steer`` here is not a fallback for it.
    """

    async def interrupt(self, session_id: str) -> bool: ...
    async def resume(self, session_id: str) -> bool: ...
    async def steer(self, session_id: str, text: str) -> bool: ...
    async def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        approved: bool,
        decision: dict[str, Any] | None = None,
    ) -> bool: ...


class HarnessToolTransport(Protocol):
    """Optional interactive tool-result channel for a live session.

    Also a seam with no implementation and no caller: every CLI provider runs its own tools, so there is no tool
    result of ours to hand back. The capability matrix has no row for it on purpose.
    """

    async def submit_tool_result(
        self,
        session_id: str,
        call_id: str,
        result: dict[str, Any],
    ) -> bool: ...


class HarnessProvider(abc.ABC):
    """Stable product boundary for Codex, runtime_v2 and other Providers."""

    provider_name: str = ""

    @abc.abstractmethod
    async def create_session(
        self,
        scope: HarnessScope,
        profile: str = "",
        context: dict[str, Any] | None = None,
    ) -> str:
        ...

    @abc.abstractmethod
    async def resume_session(self, session_id: str) -> bool:
        ...

    @abc.abstractmethod
    async def start_turn(
        self,
        session_id: str,
        instruction: str,
        inputs: dict[str, Any] | None = None,
        callbacks: HarnessCallbacks | None = None,
    ) -> TaskResult:
        ...

    async def submit_tool_result(
        self,
        session_id: str,
        call_id: str,
        result: dict[str, Any],
    ) -> bool:
        """Feed a tool result to Providers that expose interactive tools."""

        _ = (session_id, call_id, result)
        return False

    async def run_turn(
        self,
        session_id: str,
        instruction: str,
        inputs: dict[str, Any] | None = None,
        callbacks: HarnessCallbacks | None = None,
    ) -> TaskResult:
        """Canonical product spelling for one turn.

        ``start_turn`` remains the low-level compatibility name used by the
        existing adapter bridge; callers building workflows should use this
        alias so every Provider exposes the same runTurn-shaped contract.
        """
        return await self.start_turn(session_id, instruction, inputs, callbacks)

    async def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        approved: bool,
        decision: dict[str, Any] | None = None,
    ) -> HarnessOperationResult:
        """No control channel, unless a Provider builds one.

        Says so, and says where the decision does land: the durable
        checkpoint is the authority either way, and answering it through the
        product's own interaction path is not a workaround for this -- it is
        the same write this would perform.
        """
        _ = (session_id, approval_id, approved, decision)
        return unsupported_operation(
            "approval",
            alternative="answer the durable checkpoint through the interaction coordinator",
        )

    async def steer(self, session_id: str, text: str) -> HarnessOperationResult:
        _ = (session_id, text)
        return unsupported_operation(
            "steering",
            alternative="interrupt the turn and start the next one with the new instruction",
        )

    async def steer_turn(
        self, session_id: str, text: str, *, expected_turn_id: str,
    ) -> HarnessOperationResult:
        """Production steering must bind a concrete turn at the delivery boundary.

        An older, session-only transport is not an equivalent fallback: it can
        send a stale request to the next turn. Providers opt in explicitly.
        """
        return unsupported_operation(
            "turn-bound steering", alternative="refresh the session; do not resend automatically",
        )

    @abc.abstractmethod
    async def interrupt(self, session_id: str) -> bool:
        ...

    async def collect_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        _ = session_id
        return []

    @abc.abstractmethod
    async def close_session(self, session_id: str) -> bool:
        ...


@dataclass
class _Session:
    scope: HarnessScope
    task: Task
    profile: str
    context: dict[str, Any]
    active: bool = True
    # What the turns of this session left in the workspace. Kept on the
    # session rather than returned once, because the question "what did this
    # run produce" is asked after the turn, usually by somebody deciding
    # whether to ship it.
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    last_snapshot: WorkspaceSnapshot | None = None
    last_observation: dict[str, Any] = field(default_factory=dict)
    stopped_turns: list[dict[str, Any]] = field(default_factory=list)


async def _invoke_callback(callback: Callable[..., Any] | None, *args: Any) -> None:
    if not callable(callback):
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


class AdapterHarnessProvider(HarnessProvider):
    """Minimal Harness adapter over an existing ExternalAgentAdapter.

    It intentionally uses ``adapter.execute`` (and its established broker
    preflight/approval path) rather than launching a second process here.
    This makes the API safe to introduce incrementally and keeps Provider
    replacement possible.
    """

    def __init__(
        self,
        adapter: ExternalAgentAdapter,
        *,
        control_transport: HarnessControlTransport | None = None,
        tool_transport: HarnessToolTransport | None = None,
        require_conformance: bool = False,
        turn_wall_clock_sec: float = 0.0,
        collect_workspace_artifacts: bool = True,
        registry: Any = None,
    ) -> None:
        self.adapter = adapter
        self.provider_name = adapter.agent_type
        # The same registry the native bridge writes to. Without it a CLI
        # session lived only in this process's memory, and a run killed
        # mid-turn left nothing a fresh reader could rebuild from -- the
        # live Codex suite of 2026-09-09 failed a6 on exactly that.
        self.registry = registry
        self.control_transport = control_transport
        self.tool_transport = tool_transport
        self.require_conformance = bool(require_conformance)
        # A CLI turn has no natural end. Without a deadline a wedged provider
        # is indistinguishable from a slow one, and the run waits forever for
        # a process that is never going to answer. Zero keeps the old
        # behaviour, which is exactly that wait.
        self.turn_wall_clock_sec = float(turn_wall_clock_sec or 0.0)
        self.collect_workspace_artifacts = bool(collect_workspace_artifacts)
        self._sessions: dict[str, _Session] = {}
        self._running_turns: dict[str, asyncio.Task[Any]] = {}
        # Legacy CLI adapters own one process handle, not one per session.
        # Claim before callbacks/awaits so they cannot overwrite that handle.
        self._active_cli_session: str | None = None
        self._cli_turn_controller: asyncio.Task[Any] | None = None

    def conformance_report(self) -> Any:
        """Return the fail-closed production fence report for this adapter."""
        from ampuly.layer3_agent.provider_conformance import evaluate_provider

        return evaluate_provider(self.adapter)

    def ensure_production_ready(self) -> Any:
        """Raise before session creation when the Provider is not proven safe."""
        report = self.conformance_report()
        if not report.production_ready:
            blockers = ", ".join(report.blockers) or "no conformance checks passed"
            raise ValueError(f"provider {self.provider_name!r} is not production-ready: {blockers}")
        return report

    async def create_session(
        self,
        scope: HarnessScope,
        profile: str = "",
        context: dict[str, Any] | None = None,
    ) -> str:
        scope.validate()
        production_requested = self.require_conformance or bool((context or {}).get("production"))
        conformance = self.ensure_production_ready() if production_requested else None
        session_id = uuid.uuid4().hex
        if self.registry is not None:
            record = await asyncio.to_thread(
                self.registry.open_session,
                provider=self.provider_name, project_id=scope.project_id,
                customer_id=scope.customer_id, environment_id=scope.environment_id, run_id=scope.run_id,
                order_id=str((context or {}).get("order_id", "") or ""),
                task_id=str((context or {}).get("task_id", "") or ""),
                initial_context={
                    "profile": profile,
                    "workspace": scope.workspace_path,
                    # Which allowance this session's turns draw on, in
                    # the durable record rather than only in memory.
                    "budget_id": str((context or {}).get("budget_id", "") or ""),
                },
            )
            session_id = record.session_id
        task = Task(
            title=str((context or {}).get("title") or "Harness turn").strip(),
            description=str((context or {}).get("description") or "").strip(),
            project_id=scope.project_id,
            metadata={
                "harness_session_id": session_id,
                "harness_provider": self.provider_name,
                "harness_profile": profile,
                # Keep the authority tuple at the task boundary as well as in
                # ``harness_scope``.  The common paid-tool/CLI budget guard
                # validates this exact tuple before it reserves anything.
                "customer_id": scope.customer_id,
                "environment_id": scope.environment_id,
                "order_id": str((context or {}).get("order_id", "") or ""),
                "run_id": scope.run_id,
                # The budget and the home its store lives in travel on the
                # session's own task, the way the native bridge carries
                # them, so a CLI turn is not free by default.
                "budget_id": str((context or {}).get("budget_id", "") or ""),
                # The order that budget answers to, when it is not this session's own (a child order spending from
                # its parent's allowance). The common guard reads it; without it every turn of a child was refused.
                "budget_order_id": str((context or {}).get("budget_order_id", "") or ""),
                "ampuly_home": str((context or {}).get("ampuly_home", "") or ""),
                "harness_scope": {
                    "project_id": scope.project_id,
                    "run_id": scope.run_id,
                    "customer_id": scope.customer_id,
                    "environment_id": scope.environment_id,
                },
                "harness_production_requested": production_requested,
                "harness_conformance": conformance.to_payload() if conformance is not None else None,
            },
        )
        self._sessions[session_id] = _Session(scope, task, profile, dict(context or {}))
        return session_id

    async def resume_session(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None:
            return False
        if self._active_cli_session == session_id:
            return False
        if self.control_transport is not None:
            resumed = await self.control_transport.resume(session_id)
            if not resumed:
                return False
        session.active = True
        return True

    def _active_session(self, session_id: str) -> _Session | None:
        session = self._sessions.get(str(session_id or ""))
        return session if session is not None and session.active else None

    async def _record_refusal(
        self,
        session_id: str,
        operation: str,
        outcome: "HarnessOperationResult",
    ) -> None:
        """Write down why an interactive operation did not happen.

        Three unrelated situations used to answer with the same silent
        False: this provider has no such transport, the session is not
        live, or the transport refused. The first one is the standing
        truth for every CLI provider, and reporting it as a failed attempt
        is how "nobody built this" turns into "we tried and it broke".

        The caller is handed the same object that lands here, so the record
        and the return value cannot be two authorings of one fact. `reason`
        is a stable token because a program reads it; the sentence is in
        `detail`, where rewording it breaks nothing.
        """
        if self.registry is None:
            return
        try:
            await asyncio.to_thread(
                self.registry.record_event,
                str(session_id or ""),
                event_type="interactive_refused",
                data={
                    "operation": operation,
                    "provider": self.provider_name,
                    **outcome.to_payload(),
                },
            )
        except Exception:
            # A refusal that cannot be written down is still a refusal;
            # losing the note must not turn it into an exception.
            return

    def _refusal_reason(self, session_id: str, transport: Any) -> str:
        if transport is None:
            return HARNESS_UNSUPPORTED
        if self._active_session(session_id) is None:
            return HARNESS_SESSION_NOT_LIVE
        return ""

    async def submit_tool_result(
        self,
        session_id: str,
        call_id: str,
        result: dict[str, Any],
    ) -> HarnessOperationResult:
        reason = self._refusal_reason(session_id, self.tool_transport)
        if reason:
            outcome = self._refusal(
                reason,
                operation="tool result",
                alternative="run the tool through the product registry and pass the output in the next turn",
            )
            await self._record_refusal(session_id, "submit_tool_result", outcome)
            return outcome
        accepted = bool(await self.tool_transport.submit_tool_result(session_id, call_id, dict(result or {})))
        if not accepted:
            outcome = HarnessOperationResult(
                ok=False,
                reason=HARNESS_REFUSED,
                detail="the tool transport did not accept the result",
            )
            await self._record_refusal(session_id, "submit_tool_result", outcome)
            return outcome
        return HarnessOperationResult(ok=True)

    async def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        approved: bool,
        decision: dict[str, Any] | None = None,
    ) -> HarnessOperationResult:
        reason = self._refusal_reason(session_id, self.control_transport)
        if reason:
            outcome = self._refusal(
                reason,
                operation="approval",
                alternative="answer the durable checkpoint through the interaction coordinator",
            )
            await self._record_refusal(session_id, "resolve_approval", outcome)
            return outcome
        accepted = bool(
            await self.control_transport.resolve_approval(
                session_id, approval_id, bool(approved), dict(decision or {})
            )
        )
        if not accepted:
            outcome = HarnessOperationResult(
                ok=False,
                reason=HARNESS_REFUSED,
                detail="the control transport did not accept the decision",
            )
            await self._record_refusal(session_id, "resolve_approval", outcome)
            return outcome
        return HarnessOperationResult(ok=True)

    async def steer(self, session_id: str, text: str) -> HarnessOperationResult:
        reason = self._refusal_reason(session_id, self.control_transport)
        if reason:
            outcome = self._refusal(
                reason,
                operation="steering",
                alternative="interrupt the turn and start the next one with the new instruction",
            )
            await self._record_refusal(session_id, "steer", outcome)
            return outcome
        accepted = bool(await self.control_transport.steer(session_id, str(text or "")))
        if not accepted:
            outcome = HarnessOperationResult(
                ok=False,
                reason=HARNESS_REFUSED,
                detail="the control transport did not accept the steer",
            )
            await self._record_refusal(session_id, "steer", outcome)
            return outcome
        return HarnessOperationResult(ok=True)

    @staticmethod
    def _refusal(
        reason: str,
        *,
        operation: str,
        alternative: str,
    ) -> HarnessOperationResult:
        """Turn a preflight reason into the answer the caller gets.

        A missing channel carries the alternative; a session that is merely
        not live does not, because the thing to do about that is resume it,
        and suggesting a different road would send a caller away from the one
        that is about to work.
        """
        if reason == HARNESS_UNSUPPORTED:
            return unsupported_operation(operation, alternative=alternative)
        return HarnessOperationResult(
            ok=False,
            reason=reason,
            detail="the session is not live",
        )

    async def start_turn(
        self,
        session_id: str,
        instruction: str,
        inputs: dict[str, Any] | None = None,
        callbacks: HarnessCallbacks | None = None,
    ) -> TaskResult:
        if self._active_cli_session is not None:
            raise ValueError("harness adapter already owns an active turn")
        self._active_cli_session = session_id
        self._cli_turn_controller = asyncio.current_task()
        try:
            return await self._start_owned_turn(session_id, instruction, inputs, callbacks)
        finally:
            self._cli_turn_controller = None
            self._active_cli_session = None

    async def _start_owned_turn(
        self, session_id: str, instruction: str,
        inputs: dict[str, Any] | None, callbacks: HarnessCallbacks | None,
    ) -> TaskResult:
        session = self._sessions.get(session_id)
        if session is None or not session.active:
            raise ValueError("harness session is not active")
        turn_id = uuid.uuid4().hex
        task = session.task
        # The task is the Provider's mutable execution object, but its project
        # identity must remain the immutable Harness scope.  A caller cannot
        # smuggle a different project through turn inputs or resumed state.
        if str(getattr(task, "project_id", "") or "").strip() != session.scope.project_id:
            raise ValueError("harness task escaped its project scope")
        task.description = str(instruction or "").strip()
        task.metadata = {
            **dict(task.metadata or {}),
            "harness_turn_id": turn_id,
            "harness_inputs": dict(inputs or {}),
        }
        await _invoke_callback(
            getattr(callbacks, "on_event", None),
            HarnessEvent("turn_started", session_id, turn_id, {"provider": self.provider_name}),
        )
        await _invoke_callback(
            getattr(callbacks, "on_progress", None),
            f"Harness turn started ({self.provider_name})",
        )
        before = self._snapshot(session)
        stopped_reason = ""
        # Held before the claim is written: a turn that cannot be paid
        # for must not first take the exclusive claim on the session.
        guard = self._hold_a_turn(session_id, turn_id)
        if self.registry is not None:
            await asyncio.to_thread(
                self.registry.record_event, session_id, event_type="turn_started", turn_id=turn_id,
                data={"task_id": task.id, "provider": self.provider_name}, exclusive_turn=True,
            )
            if self.collect_workspace_artifacts:
                # A CLI reports nothing while it works: what it did is in the workspace. The baseline is kept WITH the
                # turn, so a turn that never ends can still be compared against the workspace afterwards.
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        self.registry.record_event, session_id, event_type="workspace_baseline", turn_id=turn_id,
                        data=baseline_record(before),
                    )
        if not session.active:
            # A callback may suspend the session before a CLI has been started.
            raise ValueError("harness session was interrupted before execution")
        running = asyncio.ensure_future(self.adapter.execute(task, session.scope.workspace_path))
        self._running_turns[session_id] = running
        # Say which process holds this turn for as long as it runs, so
        # a person looking at it elsewhere can tell "still going" from
        # "the process that had it is gone".
        beating = asyncio.ensure_future(self._beat_while_running(session_id, turn_id, running))
        try:
            if self.turn_wall_clock_sec > 0:
                result = await asyncio.wait_for(asyncio.shield(running), self.turn_wall_clock_sec)
            else:
                # Cancellation must reach this controller BEFORE the adapter
                # clears its process handle in execute's finally block.
                result = await asyncio.shield(running)
        except asyncio.TimeoutError:
            stopped_reason = f"turn exceeded {self.turn_wall_clock_sec:g}s"
            result = await self._stop_running_turn(session, running, stopped_reason)
        except asyncio.CancelledError:
            # Somebody interrupted this session. A cancelled turn still wrote
            # whatever it wrote, and saying nothing about that is how a half
            # -written delivery gets shipped.
            stopped_reason = "turn was interrupted"
            result = await self._stop_running_turn(session, running, stopped_reason)
        except Exception as exc:
            # The provider was launched; whether it spent anything is
            # unknown, so the hold stays for a person rather than being
            # quietly handed back.
            if guard is not None:
                guard.unknown(reason=f"{type(exc).__name__} after the turn was dispatched")
            if self.registry is not None:
                await self._settle_turn(session_id, turn_id, "turn_failed",
                                        {"error_type": type(exc).__name__, "error": str(exc)[:500]},
                                        state_after="suspended", state_reason=f"turn failed: {type(exc).__name__}")
            raise
        finally:
            beating.cancel()
            await asyncio.gather(beating, return_exceptions=True)
            self._running_turns.pop(session_id, None)
        # A turn that ran is a turn spent, whether it was stopped or not:
        # the provider was launched and its account was charged either way.
        if guard is not None:
            try:
                guard.settle()
            except Exception as exc:  # noqa: BLE001 - the turn ran; its result stands
                if self.registry is not None:
                    with contextlib.suppress(Exception):
                        self.registry.record_event(
                            session_id, event_type="budget_settle_failed", turn_id=turn_id,
                            data={"reason": f"{type(exc).__name__}: {str(exc)[:200]}"},
                        )
        produced = self._record_artifacts(session, before, turn_id)
        if self.registry is not None and self.collect_workspace_artifacts:
            with contextlib.suppress(Exception):
                self.registry.record_event(
                    session_id, event_type="workspace_changes", turn_id=turn_id,
                    data=changes_record(produced, scan_complete=bool(session.last_observation.get("scan_complete"))),
                )
        if self.registry is not None:
            status_value = str(getattr(getattr(result, "status", None), "value", getattr(result, "status", "")))
            await self._settle_turn(
                session_id, turn_id, "turn_cancelled" if stopped_reason else "turn_finished",
                {"status": status_value, "artifacts": len(produced), "reason": stopped_reason},
                state_after="suspended" if stopped_reason else "", state_reason=stopped_reason,
            )
        # An empty diff with a failed scan is not evidence of no file changes.
        result.artifacts = {**dict(result.artifacts or {}),
                            "workspace_observation": dict(session.last_observation)}
        if stopped_reason:
            session.stopped_turns.append({
                "turn_id": turn_id, "reason": stopped_reason, "artifacts": len(produced),
            })
            result.artifacts = {
                **dict(result.artifacts or {}),
                "harness_stopped": True,
                "harness_stopped_reason": stopped_reason,
            }
        if produced:
            # The result is saved on the Task row: the list is cut at the same bound as the recorded one, and says so.
            result.artifacts = {**dict(result.artifacts or {}), "workspace_changes": produced[:MAX_RECORDED_CHANGES],
                                **({"workspace_changes_total": len(produced)} if len(produced) > MAX_RECORDED_CHANGES else {})}
        await _invoke_callback(
            getattr(callbacks, "on_event", None),
            HarnessEvent(
                "turn_stopped" if stopped_reason
                else "turn_completed" if result.status.value in {"done", "completed"}
                else "turn_failed",
                session_id,
                turn_id,
                {
                    "status": result.status.value,
                    "workspace_changes": len(produced),
                    "workspace_observation": dict(session.last_observation),
                    **({"reason": stopped_reason} if stopped_reason else {}),
                },
            ),
        )
        await _invoke_callback(
            getattr(callbacks, "on_progress", None),
            f"Harness turn {result.status.value}",
        )
        return result

    TURN_BEAT_SEC = 5.0

    async def _beat_while_running(self, session_id: str, turn_id: str, running: asyncio.Task[Any]) -> None:
        """Hold the lease for the life of the turn, and no longer."""
        if self.registry is None:
            return
        while not running.done():
            try:
                await asyncio.to_thread(
                    self.registry.beat_turn, session_id,
                    turn_id=turn_id, period_sec=self.TURN_BEAT_SEC,
                )
            except Exception:
                # A refused beat lets the lease go stale, which reads as
                # "probably gone" -- a safer wrong answer than stopping
                # a turn because its bookkeeping failed.
                pass
            try:
                await asyncio.sleep(self.TURN_BEAT_SEC)
            except asyncio.CancelledError:
                return

    def _hold_a_turn(self, session_id: str, turn_id: str) -> Any | None:
        """Reserve one turn against the order's allowance, if it has one.

        A CLI Provider bills the operator's own account, so what can be
        held here is the number of turns this order may spend, not the
        model cost, which we never see. A session with no budget stays
        unlimited exactly as before, and says so by holding nothing
        rather than by holding zero.
        """
        session = self._sessions.get(str(session_id or ""))
        if session is None:
            return None
        from ampuly.layer4_tools.spend_guard import SpendGuard, budget_for_task

        try:
            try:
                business, budget_id = budget_for_task(session.task)
            except RuntimeError as exc:
                from ampuly.llm.budget import paid_tool_stop

                raise paid_tool_stop(exc, resource="model_call") from exc
            if business is None or not budget_id:
                return None
            budget = business.run_budget(budget_id)
            if not budget or "cost_cny" in dict(budget.get("limits", {}) or {}):
                from ampuly.llm.budget import ModelBudgetStop
                raise ModelBudgetStop("CLI turn cannot enforce a monetary ceiling; request not started")
            reservation = business.reserve_budget(
                budget_id, kind="model_call", amount=1, actor=self.provider_name,
                idempotency_key=f"{session_id}:{turn_id}", note=f"{self.provider_name} turn",
            )
        except Exception as exc:
            # A turn that cannot be held is a turn nobody is counting.
            # Say so in the record rather than proceeding silently.
            if self.registry is not None:
                try:
                    self.registry.record_event(
                        session_id, event_type="budget_hold_failed", turn_id=turn_id,
                        data={"reason": f"{type(exc).__name__}: {str(exc)[:200]}"},
                    )
                except Exception:
                    pass
            raise
        return SpendGuard(business, budget_id, str(reservation["reservation_id"]), "model_call")

    def _snapshot(self, session: _Session) -> WorkspaceSnapshot | None:
        """Stock-take before a turn, if this session is collecting at all."""
        if not self.collect_workspace_artifacts:
            return None
        try:
            return snapshot_workspace(session.scope.workspace_path)
        except OSError:
            # Not being able to look is not the same as nothing having
            # happened; the turn still runs, and the artifact list will say
            # it could not be established rather than claim it was empty.
            return None

    def _record_artifacts(
        self, session: _Session, before: WorkspaceSnapshot | None, turn_id: str,
    ) -> list[dict[str, Any]]:
        session.last_observation = {"state": "unavailable", "scan_complete": False,
                                    "basis": "size_mtime_with_exclusions"}
        if not self.collect_workspace_artifacts:
            session.last_observation["state"] = "disabled"
            return []
        if before is None:
            session.last_observation["reason"] = "baseline_unavailable"
            return []
        try:
            after = snapshot_workspace(session.scope.workspace_path)
        except OSError:
            session.last_observation["reason"] = "final_scan_unavailable"
            return []
        complete = before.complete and after.complete
        session.last_observation.update({
            "state": "complete" if complete else "partial", "scan_complete": complete,
            "before_count": before.count, "after_count": after.count,
            "truncated": before.truncated or after.truncated,
            "unreadable_count": len(before.unreadable) + len(after.unreadable),
        })
        session_id = str(session.task.metadata.get("harness_session_id", "") or "")
        produced = collect_turn_artifacts(before, after, session_id=session_id, turn_id=turn_id)
        session.last_snapshot = after
        session.artifacts.extend(produced)
        return produced

    async def _stop_running_turn(
        self, session: _Session, running: "asyncio.Task[Any]", reason: str,
    ) -> TaskResult:
        """Stop the provider and return a result that says it was stopped.

        Killing the process and raising is not a cancel: the caller learns
        that something went wrong, not that the work was deliberately ended
        and left partly done. The difference decides whether anybody goes
        looking at what is in the workspace.
        """
        task_id = str(getattr(session.task, "id", "") or "")
        try:
            await self.adapter.cancel(task_id)
        except Exception:  # noqa: BLE001 - a failed cancel must not hide the stop
            pass
        running.cancel()
        try:
            await running
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        return TaskResult(status=TaskStatus.CANCELLED, content=reason)

    async def interrupt(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None:
            return False
        if self.control_transport is not None:
            interrupted = await self.control_transport.interrupt(session_id)
            if not interrupted:
                return False
            session.active = False
            return True
        session.active = False
        running = self._running_turns.get(session_id)
        if running is not None and not running.done():
            # The turn's own handler stops the provider and records what it
            # left behind; cancelling here without that would lose the files.
            controller = self._cli_turn_controller
            if controller is None or controller.done():
                return False
            controller.cancel()
            return True
        # No execution belongs to this session. The legacy adapter's cancel
        # ignores task_id and would kill a DIFFERENT session's process.
        return True

    async def _settle_turn(self, session_id: str, turn_id: str, event_type: str, data: dict[str, Any], *,
                           state_after: str = "", state_reason: str = "") -> None:
        """Write the terminal record; a registry that will not take it is logged, not hidden."""
        try:
            await asyncio.to_thread(
                self.registry.record_event, session_id, event_type=event_type, turn_id=turn_id, data=data,
                state_after=state_after, state_reason=state_reason, settle_turn=True, lock_timeout=10.0,
            )
        except Exception as exc:  # noqa: BLE001 - the turn's own outcome keeps precedence
            logger.error("harness registry refused the terminal record for {}/{}: {}", session_id, turn_id, exc)

    async def collect_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        """Everything this session's turns left in the workspace, in order."""
        session = self._sessions.get(str(session_id or ""))
        return list(session.artifacts) if session is not None else []

    def stopped_turns(self, session_id: str) -> list[dict[str, Any]]:
        """Which turns of this session ended early, and why."""
        session = self._sessions.get(str(session_id or ""))
        return list(session.stopped_turns) if session is not None else []

    async def close_session(self, session_id: str) -> bool:
        if self._active_cli_session == session_id:
            raise ValueError("interrupt and await the active turn before closing its session")
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        session.active = False
        return True

    async def status(self) -> AgentStatus:
        return await self.adapter.get_status()
