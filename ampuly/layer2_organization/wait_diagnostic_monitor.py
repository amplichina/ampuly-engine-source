"""Run-local, bounded notice policy; supplied snapshots only, no Store reads."""

from __future__ import annotations

import math
from typing import Any, Awaitable, Callable

from ampuly.layer2_organization.company_runtime_identity import is_runtime_auxiliary_task
from ampuly.layer2_organization.wait_diagnostics import diagnose_wait_snapshot, wait_diagnostic_message

MAX_NOTICE_ATTEMPTS = 128


class WaitDiagnosticMonitor:
    """One instance per controller invocation, never shared across runs.

    Five seconds bounds ordinary ticks; an actual exit uses the final supplied
    snapshot once. Notice keys are reserved BEFORE any await, so a callback
    which writes then raises (unknown outcome) is never retried by this monitor.
    This is not an exactly-once delivery protocol.
    """

    def __init__(self) -> None:
        self._scope: tuple[str, str, str] | None = None
        self._last_checked_at: float | None = None
        self._attempted: set[str] = set()
        self._parked_observed = False

    def resume_after_delivery(self) -> None:
        """The caller observed a new delivery after a would-be exit await.

        Keep the content's attempted marker; only permit a fresh final
        observation at the next real convergence point of this invocation.
        """
        self._parked_observed = False

    @staticmethod
    def _task_route(tasks: list[Any], observation: dict[str, Any]) -> str | None:
        scope = observation["scope"]
        if observation["status"] == "invalid" or not scope:
            return None
        valid_ids = []
        for task in tasks:
            metadata = getattr(task, "metadata", None)
            if type(metadata) is not dict:
                continue
            if is_runtime_auxiliary_task(task):
                continue
            root = (
                getattr(task, "parent_session_id", None)
                or metadata.get("company_runtime_root_session_id")
                or metadata.get("parent_session_id")
                or getattr(task, "session_id", None)
            )
            if (
                getattr(task, "project_id", None) == scope["project_id"]
                and metadata.get("delegation_run_id") == scope["run_id"]
                and root == scope["runtime_session_id"]
            ):
                tid = getattr(task, "id", None)
                if type(tid) is str and 0 < len(tid) <= 256 and tid == tid.strip() and not any(ord(c) < 32 or ord(c) == 127 for c in tid):
                    valid_ids.append(tid)
        involved = {tid for edge in observation["candidate_cycle_edges"] for tid in edge}
        return next((tid for tid in valid_ids if tid in involved), valid_ids[0] if valid_ids else None)

    async def observe(
        self,
        tasks: list[Any],
        work_items: list[Any],
        *,
        project_id: str,
        runtime_session_id: str,
        run_id: str,
        busy_task_ids: list[str] | set[str] | tuple[str, ...] = (),
        now: float,
        emit_progress: Callable[..., Awaitable[None]],
        parked: bool = False,
    ) -> str:
        """Return a fixed outcome code; ordinary failures cannot drive dispatch.

        Caller supplies monotonic time and its trusted controller scope. Only
        call ``parked=True`` at the existing actual return/break boundary. The
        caller still owns the post-await delivery-count/convergence checks.
        """
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            return "clock_invalid"
        scope = (project_id, runtime_session_id, run_id)
        if self._scope is None:
            self._scope = scope
        elif self._scope != scope:
            return "scope_changed"
        if parked:
            if self._parked_observed:
                return "parked_already_observed"
            self._parked_observed = True
        elif self._last_checked_at is not None and now - self._last_checked_at < 5.0:
            return "not_due"
        self._last_checked_at = float(now)
        try:
            observation = diagnose_wait_snapshot(
                tasks, work_items, project_id=project_id,
                runtime_session_id=runtime_session_id, run_id=run_id,
                busy_task_ids=busy_task_ids,
            )
            message = wait_diagnostic_message(observation, include_wait_reasons=parked)
            if not message:
                return "no_notice"
            # Deduplicate identical content even if the final observation's
            # status fingerprint differs but its bounded notice is unchanged.
            key = message
            if key in self._attempted:
                return "unchanged"
            if len(self._attempted) >= MAX_NOTICE_ATTEMPTS:
                return "notice_limit"
            task_id = self._task_route(tasks, observation)
            if not task_id:
                return "route_unavailable"
            self._attempted.add(key)
            try:
                await emit_progress(message, task_id=task_id)
            except Exception:
                return "outcome_unknown"
            return "notified"
        except Exception:
            return "diagnostic_failed"
