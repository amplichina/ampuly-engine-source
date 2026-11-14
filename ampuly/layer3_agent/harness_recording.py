"""Give any Harness provider the same durable record as the native one.

The adapter bridge already reaches Codex, Claude Code and OpenCode through
the Harness contract, but its sessions live in memory: restart the console
and a running CLI turn becomes an orphan nobody can attribute or resume. The
native bridge writes its sessions and events down; every other provider
deserves the same, and for the same reason -- otherwise "which run was that"
has no answer for exactly the providers most likely to be running.

This wraps a provider rather than editing it, so the wrapped implementation
stays the one under test elsewhere, and any future provider gets the record
by being wrapped rather than by remembering to implement it.

It records; it never invents. If the wrapped provider says an interrupt did
not happen, that is what the record says.
"""

from __future__ import annotations

from typing import Any

from ampuly.core.models import TaskResult, TaskStatus
from ampuly.layer3_agent.harness import (
    HARNESS_NOT_FOUND,
    HARNESS_REFUSED,
    HarnessCallbacks,
    HarnessOperationResult,
    HarnessProvider,
    HarnessScope,
)
from ampuly.layer3_agent.harness_sessions import HarnessSessionLost, HarnessSessionRegistry


def _as_result(value: Any) -> HarnessOperationResult:
    """Accept either the structured result or the boolean it replaced.

    This wraps providers, including ones written elsewhere and ones written
    before the result type existed. A bare ``False`` becomes ``refused`` and
    not ``unsupported``: a boolean only ever said "no", and upgrading that to
    "this provider has no such channel" would be inventing a reason, which is
    the one thing this wrapper does not do.
    """
    if isinstance(value, HarnessOperationResult):
        return value
    if value:
        return HarnessOperationResult(ok=True)
    return HarnessOperationResult(
        ok=False,
        reason=HARNESS_REFUSED,
        detail="the wrapped provider answered with a bare False",
    )


class RecordingHarnessProvider(HarnessProvider):
    """A Harness provider plus the durable session and event record."""

    def __init__(self, inner: HarnessProvider, registry: HarnessSessionRegistry) -> None:
        self.inner = inner
        self.registry = registry
        self.provider_name = getattr(inner, "provider_name", "") or inner.__class__.__name__
        # Ours -> the wrapped provider's. The two id spaces meet here and
        # nowhere else, so nothing downstream depends on a provider's shape.
        self._inner_ids: dict[str, str] = {}

    # ── sessions ──

    async def create_session(
        self,
        scope: HarnessScope,
        profile: str = "",
        context: dict[str, Any] | None = None,
    ) -> str:
        scope.validate()
        inner_id = await self.inner.create_session(scope, profile, context)
        record = self.registry.open_session(
            provider=self.provider_name,
            project_id=scope.project_id,
            customer_id=scope.customer_id,
            environment_id=scope.environment_id,
            run_id=scope.run_id,
            order_id=str((context or {}).get("order_id", "") or ""),
            task_id=str((context or {}).get("task_id", "") or ""),
            provider_session_id=str(inner_id),
        )
        self._inner_ids[record.session_id] = str(inner_id)
        self.registry.record_event(record.session_id, event_type="session_created", data={"profile": profile})
        return record.session_id

    async def resume_session(self, session_id: str) -> bool:
        inner_id = self._inner_id(session_id)
        resumed = bool(await self.inner.resume_session(inner_id)) if inner_id else False
        if not resumed:
            # The provider could not continue it; recording "resumed" here
            # would be the console telling itself a comfortable story.
            self.registry.record_event(session_id, event_type="resume_failed")
            return False
        try:
            self.registry.set_state(session_id, "active", reason="resumed")
        except HarnessSessionLost:
            return False
        self.registry.record_event(session_id, event_type="session_resumed")
        return True

    async def close_session(self, session_id: str) -> bool:
        inner_id = self._inner_id(session_id)
        closed = bool(await self.inner.close_session(inner_id)) if inner_id else False
        self.registry.set_state(session_id, "closed", reason="closed by caller")
        self._inner_ids.pop(session_id, None)
        return closed

    async def interrupt(self, session_id: str) -> bool:
        inner_id = self._inner_id(session_id)
        stopped = bool(await self.inner.interrupt(inner_id)) if inner_id else False
        if stopped:
            self.registry.record_event(session_id, event_type="interrupted")
            self.registry.set_state(session_id, "suspended", reason="interrupted by a person")
        else:
            self.registry.record_event(session_id, event_type="interrupt_unsupported")
        return stopped

    # ── one turn ──

    async def start_turn(
        self,
        session_id: str,
        instruction: str,
        inputs: dict[str, Any] | None = None,
        callbacks: HarnessCallbacks | None = None,
    ) -> TaskResult:
        record = self.registry.session(session_id)
        if record is None:
            raise ValueError(f"unknown session {session_id!r}")
        if record.state == "lost":
            raise HarnessSessionLost(f"session {session_id!r} was lost; start a new one")
        inner_id = self._inner_id(session_id)
        if not inner_id:
            raise HarnessSessionLost(f"session {session_id!r} has no provider session; resume it first")

        turn_id = f"turn-{record.last_seq + 1}"
        self.registry.record_event(session_id, event_type="turn_started", turn_id=turn_id, data={"instruction": instruction})

        recorder = _Recording(self.registry, session_id, turn_id, callbacks)
        try:
            result = await self.inner.start_turn(inner_id, instruction, inputs, recorder)
        except Exception as exc:
            self.registry.record_event(
                session_id, event_type="turn_failed", turn_id=turn_id,
                data={"error_type": type(exc).__name__, "error": str(exc)[:500]},
            )
            self.registry.set_state(session_id, "suspended", reason=f"turn failed: {type(exc).__name__}")
            raise

        status = getattr(result, "status", None)
        self.registry.record_event(
            session_id, event_type="turn_finished", turn_id=turn_id,
            data={"status": getattr(status, "value", str(status))},
        )
        if status == TaskStatus.AWAITING_HUMAN:
            self.registry.set_state(session_id, "suspended", reason="waiting for a person")
        return result

    async def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        approved: bool,
        decision: dict[str, Any] | None = None,
    ) -> HarnessOperationResult:
        inner_id = self._inner_id(session_id)
        outcome = (
            await self.inner.resolve_approval(inner_id, approval_id, approved, decision)
            if inner_id
            else HarnessOperationResult(
                ok=False,
                reason=HARNESS_NOT_FOUND,
                detail="this recording knows no inner session",
            )
        )
        outcome = _as_result(outcome)
        # The reason goes into the event, not just the return value. A
        # recording read back later has to be able to say whether an approval
        # failed because nobody built the channel or because the session had
        # already ended -- flattening both to "failed" is how a replay
        # reproduces the wrong thing.
        self.registry.record_event(
            session_id,
            event_type="approval_resolved" if outcome.ok else "approval_resolution_failed",
            data={"approval_id": approval_id, "approved": bool(approved), **outcome.to_payload()},
        )
        return outcome

    async def steer(self, session_id: str, text: str) -> HarnessOperationResult:
        inner_id = self._inner_id(session_id)
        outcome = _as_result(
            await self.inner.steer(inner_id, text)
            if inner_id
            else HarnessOperationResult(
                ok=False,
                reason=HARNESS_NOT_FOUND,
                detail="this recording knows no inner session",
            )
        )
        self.registry.record_event(
            session_id,
            event_type="steered" if outcome.ok else "steer_unsupported",
            data={"text": text[:200], **outcome.to_payload()},
        )
        return outcome

    async def collect_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        inner_id = self._inner_id(session_id)
        return list(await self.inner.collect_artifacts(inner_id)) if inner_id else []

    # ── helpers ──

    def _inner_id(self, session_id: str) -> str:
        known = self._inner_ids.get(session_id)
        if known:
            return known
        # After a restart the mapping is on disk, not in this object.
        record = self.registry.session(session_id)
        return str(record.provider_session_id) if record is not None else ""


class _Recording:
    """Passes callbacks through and writes each event down on the way."""

    def __init__(
        self,
        registry: HarnessSessionRegistry,
        session_id: str,
        turn_id: str,
        inner: HarnessCallbacks | None,
    ) -> None:
        self.registry = registry
        self.session_id = session_id
        self.turn_id = turn_id
        self.inner = inner

    def on_event(self, event: Any) -> Any:
        self.registry.record_event(
            self.session_id,
            event_type=str(getattr(event, "event_type", "") or "event"),
            turn_id=self.turn_id,
            data=dict(getattr(event, "data", {}) or {}),
        )
        handler = getattr(self.inner, "on_event", None)
        return handler(event) if callable(handler) else None

    def on_progress(self, text: str) -> Any:
        self.registry.record_event(
            self.session_id, event_type="progress", turn_id=self.turn_id, data={"text": str(text)[:2000]},
        )
        handler = getattr(self.inner, "on_progress", None)
        return handler(text) if callable(handler) else None
