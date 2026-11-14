"""A Harness provider that runs offline, so the main chain can be built now.

F1 asks for runs that actually go through the unified Harness -- events,
approval pauses, resume, interrupt, artifacts -- and today every Provider that
could do that needs an account. So this one uses the scripted model from M0
and the session registry, and implements the same ``HarnessProvider``
contract a real Provider will.

It is not a stand-in for a real Provider's behaviour and never claims to be:
it exists to exercise *our* half -- scope validation, the event sequence, the
approval pause, resume after a restart, and refusing to continue a lost
session. The real ones are F3, and their conformance is F2.

Refuses to exist outside test mode.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from ampuly.core.models import Task, TaskResult, TaskStatus
from ampuly.layer3_agent.harness import (
    HarnessCallbacks,
    HarnessEvent,
    HarnessProvider,
    HarnessScope,
    _invoke_callback,
)
from ampuly.layer3_agent.harness_sessions import HarnessSessionLost, HarnessSessionRegistry
from ampuly.llm.deterministic import (
    TEST_MODE_ENV,
    DeterministicProvider,
    DeterministicScript,
    TestModeRefused,
    test_mode_enabled,
)
from ampuly.core.config import LLMConfig

PROVIDER_NAME = "deterministic"


class DeterministicHarnessProvider(HarnessProvider):
    """One turn, scripted, with every event written down as it happens."""

    provider_name = PROVIDER_NAME

    def __init__(
        self,
        registry: HarnessSessionRegistry,
        script: DeterministicScript | None = None,
        *,
        approval_required_tools: tuple[str, ...] = (),
    ) -> None:
        if not test_mode_enabled():
            raise TestModeRefused(f"the scripted harness provider needs {TEST_MODE_ENV}=1")
        self.registry = registry
        self.llm = DeterministicProvider(LLMConfig(default_model="deterministic/harness", max_tokens=512), script=script)
        # Tools a person must approve before they run, mirroring what a real
        # Provider's approval gate does. Empty means nothing pauses.
        self.approval_required_tools = tuple(approval_required_tools)
        self._pending: dict[str, dict[str, Any]] = {}
        self._artifacts: dict[str, list[dict[str, Any]]] = {}

    # ── sessions ──

    async def create_session(
        self,
        scope: HarnessScope,
        profile: str = "",
        context: dict[str, Any] | None = None,
    ) -> str:
        scope.validate()
        record = self.registry.open_session(
            provider=self.provider_name,
            project_id=scope.project_id,
            customer_id=scope.customer_id,
            environment_id=scope.environment_id,
            run_id=scope.run_id,
            order_id=str((context or {}).get("order_id", "") or ""),
            task_id=str((context or {}).get("task_id", "") or ""),
        )
        # A real Provider hands its own id back; this one is honest that the
        # id is ours, so nothing downstream can mistake it for a remote thread.
        self.registry.bind_provider_session(record.session_id, f"scripted:{uuid.uuid4().hex[:8]}")
        self.registry.record_event(record.session_id, event_type="session_created", data={"profile": profile})
        return record.session_id

    async def resume_session(self, session_id: str) -> bool:
        try:
            self.registry.set_state(session_id, "active", reason="resumed")
        except HarnessSessionLost:
            return False
        self.registry.record_event(session_id, event_type="session_resumed")
        return True

    async def close_session(self, session_id: str) -> bool:
        self.registry.set_state(session_id, "closed", reason="closed by caller")
        return True

    async def interrupt(self, session_id: str) -> bool:
        record = self.registry.session(session_id)
        if record is None or record.state != "active":
            return False
        self.registry.record_event(session_id, event_type="interrupted")
        self.registry.set_state(session_id, "suspended", reason="interrupted by a person")
        return True

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
        if record.state != "active":
            raise ValueError(f"session {session_id!r} is {record.state}")

        turn_id = f"turn-{uuid.uuid4().hex[:8]}"
        self.registry.record_event(
            session_id,
            event_type="turn_started",
            turn_id=turn_id,
            data={"instruction": instruction},
            exclusive_turn=True,
        )
        await self._emit(callbacks, session_id, turn_id, "turn_started", {"instruction": instruction})

        try:
            reply = await self.llm.chat([{"role": "user", "content": instruction}])
        except Exception as exc:
            # A scripted transport error represents a known failed call, not
            # an unknown dispatch outcome.  Settle the durable turn claim
            # before propagating it so recovery and RSI evaluation see the
            # same terminal fact that a production Provider must record.
            self.registry.record_event(
                session_id,
                event_type="turn_failed",
                turn_id=turn_id,
                data={
                    "status": "failed",
                    "outcome_known": True,
                    "error_type": type(exc).__name__,
                },
                settle_turn=True,
            )
            raise
        text = str(reply.get("content", "") or "")
        if text:
            self.registry.record_event(session_id, event_type="assistant_delta", turn_id=turn_id, data={"text": text})
            await _invoke_callback(getattr(callbacks, "on_progress", None), text)

        for call in list(reply.get("tool_calls", []) or []):
            name = str(call.get("function", "") or "")
            if name in self.approval_required_tools:
                approval_id = f"ap-{uuid.uuid4().hex[:8]}"
                self._pending[approval_id] = {"session_id": session_id, "turn_id": turn_id, "call": call}
                self.registry.record_event(
                    session_id, event_type="approval_required", turn_id=turn_id,
                    data={"approval_id": approval_id, "tool": name},
                )
                await self._emit(callbacks, session_id, turn_id, "approval_required", {"approval_id": approval_id, "tool": name})
                # The turn stops here on purpose: a paused turn that reported
                # success would be a lie the approval exists to prevent.
                self.registry.set_state(session_id, "suspended", reason=f"waiting for approval of {name}")
                return TaskResult(
                    status=TaskStatus.AWAITING_HUMAN,
                    content=text,
                    artifacts={"approval_id": approval_id, "tool": name, "turn_id": turn_id, "session_id": session_id},
                )
            self.registry.record_event(
                session_id, event_type="tool_call", turn_id=turn_id, data={"tool": name, "arguments": call.get("arguments", {})},
            )
            self._artifacts.setdefault(session_id, []).append({"kind": "tool_call", "tool": name, "turn_id": turn_id})

        self.registry.record_event(
            session_id,
            event_type="turn_finished",
            turn_id=turn_id,
            data={
                "status": "done",
                "chars": len(text),
                "artifacts": list(self._artifacts.get(session_id, [])),
            },
            settle_turn=True,
        )
        return TaskResult(
            status=TaskStatus.DONE,
            content=text,
            artifacts={"turn_id": turn_id, "session_id": session_id},
        )

    async def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        approved: bool,
        decision: dict[str, Any] | None = None,
    ) -> bool:
        pending = self._pending.pop(str(approval_id), None)
        if pending is None or pending["session_id"] != session_id:
            return False
        name = str(pending["call"].get("function", "") or "")
        self.registry.record_event(
            session_id, event_type="approval_resolved", turn_id=pending["turn_id"],
            data={"approval_id": approval_id, "approved": bool(approved), "tool": name, "decision": dict(decision or {})},
        )
        self.registry.set_state(session_id, "active", reason="approval resolved")
        if approved:
            self.registry.record_event(session_id, event_type="tool_call", turn_id=pending["turn_id"], data={"tool": name})
            self._artifacts.setdefault(session_id, []).append({"kind": "tool_call", "tool": name, "turn_id": pending["turn_id"]})
        else:
            self.registry.record_event(session_id, event_type="tool_refused", turn_id=pending["turn_id"], data={"tool": name})
        self.registry.record_event(
            session_id,
            event_type="turn_finished",
            turn_id=pending["turn_id"],
            data={
                "status": "done" if approved else "failed",
                "artifacts": list(self._artifacts.get(session_id, [])),
            },
            settle_turn=True,
        )
        return True

    async def steer(self, session_id: str, text: str) -> bool:
        record = self.registry.session(session_id)
        if record is None or record.state != "active":
            return False
        self.registry.record_event(session_id, event_type="steered", data={"text": text})
        return True

    async def collect_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._artifacts.get(session_id, []))

    # ── helpers ──

    async def _emit(
        self,
        callbacks: HarnessCallbacks | None,
        session_id: str,
        turn_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        await _invoke_callback(
            getattr(callbacks, "on_event", None),
            HarnessEvent(event_type=event_type, session_id=session_id, turn_id=turn_id, data=data),
        )


def build(registry_path: str | Path, script: DeterministicScript | None = None, **kwargs: Any) -> DeterministicHarnessProvider:
    return DeterministicHarnessProvider(HarnessSessionRegistry(registry_path), script=script, **kwargs)
