"""Durable, hash-chained replay cursor for one admitted browser candidate.

The Gateway journal owns individual browser actions.  This journal owns the
higher-level replay causality: which admitted candidate/run/task was active,
which steps were confirmed, and whether a crash left a step in flight.  It
stores no typed input, page body, credential, or screenshot.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ampuly.computer_use.workflow_contracts import RunBinding, WorkflowRefused


SCHEMA = "superones.replay-run.v1"
TERMINAL = frozenset({"complete", "stopped"})


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_id(value: str, label: str) -> str:
    clean = str(value or "").strip()
    if not clean or clean in {".", ".."} or len(clean) > 180:
        raise WorkflowRefused(f"{label}_invalid", clean[:80])
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in clean):
        raise WorkflowRefused(f"{label}_invalid", clean[:80])
    return clean


def replay_runs_dir() -> Path:
    from ampuly.core.config import get_ampuly_home

    return get_ampuly_home() / "computer_use" / "replay-runs"


@dataclass(frozen=True)
class ReplayRunState:
    run_id: str
    candidate_id: str
    candidate_digest: str
    admission_id: str
    scope_digest: str
    task_id: str
    binding_digest: str
    status: str
    position: int
    in_flight_step: int = 0
    effect_id: str = ""
    checkpoint_id: str = ""
    reason: str = ""
    last_event_hash: str = ""
    events: int = 0


class ReplayRunJournal:
    """One append-only JSONL chain per replay run."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else replay_runs_dir()

    def _path(self, run_id: str) -> Path:
        return self.directory / f"{_safe_id(run_id, 'run_id')}.jsonl"

    @staticmethod
    def binding_digest(binding_key: str) -> str:
        return hashlib.sha256(str(binding_key or "").encode("utf-8")).hexdigest()

    def begin(self, run: RunBinding, *, task_id: str, binding_key: str) -> ReplayRunState:
        path = self._path(run.run_id)
        if path.exists():
            state = self.load(run.run_id)
            self.assert_binding(state, run, task_id=task_id, binding_key=binding_key)
            return state
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "schema": SCHEMA,
            "sequence": 1,
            "type": "run_started",
            "run_id": run.run_id,
            "candidate_id": run.candidate.candidate_id,
            "candidate_digest": run.candidate.digest,
            "admission_id": run.admission.admission_id,
            "scope_digest": run.scope_digest,
            "task_id": _safe_id(task_id, "task_id"),
            "binding_digest": self.binding_digest(binding_key),
            "position": 0,
            "previous_hash": "",
        }
        event["event_hash"] = hashlib.sha256(_canonical(event)).hexdigest()
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            state = self.load(run.run_id)
            self.assert_binding(state, run, task_id=task_id, binding_key=binding_key)
            return state
        return self.load(run.run_id)

    def append(self, run_id: str, event_type: str, **facts: Any) -> ReplayRunState:
        state = self.load(run_id)
        if state.status in TERMINAL:
            raise WorkflowRefused("replay_run_terminal", state.status)
        event = {
            "schema": SCHEMA,
            "sequence": state.events + 1,
            "type": str(event_type),
            "run_id": state.run_id,
            "position": int(facts.pop("position", state.position)),
            "previous_hash": state.last_event_hash,
            **{key: value for key, value in facts.items() if value not in (None, "")},
        }
        event["event_hash"] = hashlib.sha256(_canonical(event)).hexdigest()
        path = self._path(run_id)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return self.load(run_id)

    def load(self, run_id: str) -> ReplayRunState:
        path = self._path(run_id)
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise WorkflowRefused("replay_journal_unreadable", type(exc).__name__) from None
        if not raw_lines:
            raise WorkflowRefused("replay_journal_unreadable", "empty")
        events: list[dict[str, Any]] = []
        previous = ""
        for expected_sequence, line in enumerate(raw_lines, start=1):
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                raise WorkflowRefused("replay_journal_unreadable", f"line {expected_sequence}") from None
            if not isinstance(event, dict) or event.get("schema") != SCHEMA:
                raise WorkflowRefused("replay_journal_unreadable", f"schema {expected_sequence}")
            if int(event.get("sequence", 0) or 0) != expected_sequence or str(event.get("previous_hash", "")) != previous:
                raise WorkflowRefused("replay_journal_chain_broken", str(expected_sequence))
            claimed_hash = str(event.get("event_hash", "") or "")
            unsigned = {key: value for key, value in event.items() if key != "event_hash"}
            if not claimed_hash or hashlib.sha256(_canonical(unsigned)).hexdigest() != claimed_hash:
                raise WorkflowRefused("replay_journal_chain_broken", str(expected_sequence))
            previous = claimed_hash
            events.append(event)

        first = events[0]
        if first.get("type") != "run_started" or str(first.get("run_id", "")) != _safe_id(run_id, "run_id"):
            raise WorkflowRefused("replay_journal_identity_mismatch")
        status = "running"
        position = 0
        in_flight = 0
        effect_id = checkpoint_id = reason = ""
        for event in events[1:]:
            kind = str(event.get("type", "") or "")
            if kind == "step_started":
                in_flight = int(event.get("step", 0) or 0)
            elif kind == "step_confirmed":
                step = int(event.get("step", 0) or 0)
                if step != in_flight or step != position + 1:
                    raise WorkflowRefused("replay_journal_chain_broken", f"step {step}")
                position, in_flight = step, 0
                status = "running"
                effect_id = checkpoint_id = ""
            elif kind == "paused_for_approval":
                status = "paused_for_approval"
                effect_id = str(event.get("effect_id", "") or "")
                checkpoint_id = str(event.get("checkpoint_id", "") or "")
            elif kind == "stopped":
                status = "stopped"
                reason = str(event.get("reason", "") or "")[:200]
            elif kind == "complete":
                status = "complete"
                position = int(event.get("position", position) or position)
            else:
                raise WorkflowRefused("replay_journal_unreadable", kind[:80])
        if status == "running" and in_flight:
            status = "outcome_unknown"
            reason = "step_started_without_confirmation"
        return ReplayRunState(
            run_id=str(first["run_id"]), candidate_id=str(first.get("candidate_id", "")),
            candidate_digest=str(first.get("candidate_digest", "")), admission_id=str(first.get("admission_id", "")),
            scope_digest=str(first.get("scope_digest", "")), task_id=str(first.get("task_id", "")),
            binding_digest=str(first.get("binding_digest", "")), status=status, position=position,
            in_flight_step=in_flight, effect_id=effect_id, checkpoint_id=checkpoint_id, reason=reason,
            last_event_hash=previous, events=len(events),
        )

    def assert_binding(self, state: ReplayRunState, run: RunBinding, *, task_id: str, binding_key: str) -> None:
        expected = (
            run.candidate.candidate_id, run.candidate.digest, run.admission.admission_id,
            run.scope_digest, _safe_id(task_id, "task_id"), self.binding_digest(binding_key),
        )
        actual = (
            state.candidate_id, state.candidate_digest, state.admission_id,
            state.scope_digest, state.task_id, state.binding_digest,
        )
        if actual != expected:
            raise WorkflowRefused("replay_run_binding_mismatch", run.run_id)

    @staticmethod
    def assert_effect(state: ReplayRunState, effect: Any) -> str:
        """Return the authoritative Effect state when it belongs to this run."""

        if str(getattr(effect, "effect_id", "") or "") != state.effect_id:
            raise WorkflowRefused("replay_effect_mismatch", state.run_id)
        if str(getattr(effect, "causation_id", "") or "") != state.run_id:
            raise WorkflowRefused("replay_effect_causation_mismatch", state.run_id)
        raw_state = getattr(effect, "state", "")
        return str(getattr(raw_state, "value", raw_state) or "").strip()
