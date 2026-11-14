"""Demonstration -> candidate -> admitted run contracts (4.3 X07, first slice: the contracts and their refusals).

Adopted from OpenAdapt Flow's running contract (OpenAdaptAI/openadapt-flow at
be44b4a, MIT; mechanism reference only, no code taken): a recorded
demonstration is sealed; a candidate is compiled from a sealed demonstration
and carries its own digest; a candidate is admitted by SuperOnes (never by
the compiler, never by a worker); a replay is deterministic and stops on
drift instead of guessing; a run exists only when it is bound to an
admission, a scope and an Effect claim. Written in SuperOnes terms so there
is one fact domain: the scope, approval, budget and Effect facts are ours.

What this slice refuses (C19, C20, G12):

- a candidate compiled from an unsealed or tampered demonstration;
- an admission by anyone but the product's admission authority, or of a
  candidate whose digest changed since;
- a run without admission, scope or Effect binding, or whose binding names
  a different candidate;
- a replay step whose observation does not match the step's expected
  observation (drift -> stop, never a guessed click);
- a step that is an external effect without an approval reference.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ampuly.computer_use.contracts import ACTION_KINDS, ActionClass, ContractRefused

ADMISSION_AUTHORITY = "superones_workflow_admission"


def _digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()).hexdigest()


def workflow_scope_digest(*, project_id: str, customer_id: str, environment_id: str) -> str:
    """Scope shared by a recorded workflow and a run: project, customer and environment, never task/session."""

    return _digest({"scope": "superones.workflow/v1", "project_id": str(project_id or ""),
                    "customer_id": str(customer_id or ""), "environment_id": str(environment_id or "")})


class WorkflowRefused(ContractRefused):
    """A contract violation on the demonstration/candidate/run path. `reason` is a stable code."""


# ── demonstration ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DemoStep:
    """One recorded step: what was observed (a hash), what was done (a contract kind), what it should have
    produced (a hash). Never the page, never typed text: the recorder redacts before the demo is sealed."""

    sequence: int
    observation_hash: str
    kind: str
    target: dict[str, Any]
    expected_after_hash: str
    intent: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ACTION_KINDS:
            raise WorkflowRefused("unknown_action_kind", self.kind)
        if "text" in self.target or "value" in self.target or "password" in self.target:
            raise WorkflowRefused("demo_step_carries_raw_input", "a sealed demo holds lengths and refs, not text")


@dataclass(frozen=True)
class SealedDemo:
    demo_id: str
    scope_digest: str
    steps: tuple[DemoStep, ...]
    recorded_by: str
    seal: str = ""

    @staticmethod
    def _seal_of(demo_id: str, scope_digest: str, steps: tuple[DemoStep, ...], recorded_by: str) -> str:
        return _digest({"demo_id": demo_id, "scope_digest": scope_digest, "recorded_by": recorded_by,
                        "steps": [s.__dict__ for s in steps]})

    @classmethod
    def seal(cls, *, demo_id: str, scope_digest: str, steps: list[DemoStep], recorded_by: str) -> "SealedDemo":
        if not steps:
            raise WorkflowRefused("demo_without_steps")
        ordered = tuple(sorted(steps, key=lambda s: s.sequence))
        if [s.sequence for s in ordered] != list(range(1, len(ordered) + 1)):
            raise WorkflowRefused("demo_steps_not_contiguous")
        return cls(demo_id=demo_id, scope_digest=scope_digest, steps=ordered, recorded_by=recorded_by,
                   seal=cls._seal_of(demo_id, scope_digest, ordered, recorded_by))

    @property
    def sealed(self) -> bool:
        return bool(self.seal) and self.seal == self._seal_of(self.demo_id, self.scope_digest, self.steps, self.recorded_by)


# ── candidate ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CandidateStep:
    sequence: int
    kind: str
    target: dict[str, Any]
    expected_before_hash: str
    expected_after_hash: str
    verification_sources: tuple[str, ...]
    intent: str = ""

    @property
    def risk(self) -> ActionClass:
        return ACTION_KINDS[self.kind]


@dataclass(frozen=True)
class CompiledCandidate:
    candidate_id: str
    demo_id: str
    demo_seal: str
    scope_digest: str
    steps: tuple[CandidateStep, ...]
    compiler_version: str
    digest: str = ""

    @staticmethod
    def _digest_of(candidate_id: str, demo_seal: str, scope_digest: str, steps: tuple[CandidateStep, ...], compiler_version: str) -> str:
        return _digest({"candidate_id": candidate_id, "demo_seal": demo_seal, "scope_digest": scope_digest,
                        "compiler_version": compiler_version, "steps": [s.__dict__ for s in steps]})

    @property
    def intact(self) -> bool:
        return bool(self.digest) and self.digest == self._digest_of(self.candidate_id, self.demo_seal, self.scope_digest, self.steps, self.compiler_version)


def compile_candidate(demo: SealedDemo, *, candidate_id: str, compiler_version: str) -> CompiledCandidate:
    """A candidate is only ever compiled from a sealed demonstration, step for step, with a
    verification plan per external effect. Nothing is invented: a step the demo did not show does not appear."""
    if not demo.sealed:
        raise WorkflowRefused("demo_not_sealed_or_tampered", demo.demo_id)
    steps = []
    for step in demo.steps:
        risk = ACTION_KINDS[step.kind]
        sources = ("url", "element") if risk is ActionClass.EXTERNAL_EFFECT else ("element",)
        steps.append(CandidateStep(sequence=step.sequence, kind=step.kind, target=dict(step.target),
                                   expected_before_hash=step.observation_hash, expected_after_hash=step.expected_after_hash,
                                   verification_sources=sources, intent=step.intent))
    ordered = tuple(steps)
    return CompiledCandidate(candidate_id=candidate_id, demo_id=demo.demo_id, demo_seal=demo.seal, scope_digest=demo.scope_digest,
                             steps=ordered, compiler_version=compiler_version,
                             digest=CompiledCandidate._digest_of(candidate_id, demo.seal, demo.scope_digest, ordered, compiler_version))


# ── admission ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CandidateAdmission:
    admission_id: str
    candidate_id: str
    candidate_digest: str
    scope_digest: str
    admitted_by: str
    authority: str
    approval_refs: dict[int, str] = field(default_factory=dict)  # step sequence -> approval reference for external effects

    def covers(self, candidate: CompiledCandidate) -> bool:
        return (self.authority == ADMISSION_AUTHORITY and self.candidate_id == candidate.candidate_id
                and self.candidate_digest == candidate.digest and self.scope_digest == candidate.scope_digest and candidate.intact)


def admit_candidate(candidate: CompiledCandidate, *, admission_id: str, admitted_by: str, authority: str,
                    approval_refs: dict[int, str] | None = None) -> CandidateAdmission:
    """Only the product's admission authority admits; a candidate that changed since compile is refused;
    every external-effect step needs an approval reference before the candidate may run at all."""
    if authority != ADMISSION_AUTHORITY:
        raise WorkflowRefused("admission_authority_not_ours", authority)
    if not candidate.intact:
        raise WorkflowRefused("candidate_digest_changed", candidate.candidate_id)
    refs = dict(approval_refs or {})
    missing = [s.sequence for s in candidate.steps if s.risk is ActionClass.EXTERNAL_EFFECT and not refs.get(s.sequence)]
    if missing:
        raise WorkflowRefused("external_effect_step_without_approval", ",".join(str(m) for m in missing))
    return CandidateAdmission(admission_id=admission_id, candidate_id=candidate.candidate_id, candidate_digest=candidate.digest,
                              scope_digest=candidate.scope_digest, admitted_by=admitted_by, authority=authority, approval_refs=refs)


# ── run binding and deterministic replay ─────────────────────────────────

@dataclass(frozen=True)
class RunBinding:
    run_id: str
    candidate: CompiledCandidate
    admission: CandidateAdmission
    scope_digest: str
    effect_claim: str

    def __post_init__(self) -> None:
        if not self.admission.covers(self.candidate):
            raise WorkflowRefused("run_without_valid_admission", self.candidate.candidate_id)
        if self.scope_digest != self.candidate.scope_digest:
            raise WorkflowRefused("run_scope_mismatch")
        if not str(self.effect_claim or "").strip():
            raise WorkflowRefused("run_without_effect_claim")


@dataclass
class ReplayCursor:
    """Deterministic replay: each step runs only when the observation is the one the candidate expects.
    Drift stops the replay with the step named (C19); nothing is guessed, nothing is retried (C20 repairs)."""

    binding: RunBinding
    position: int = 0
    stopped: str = ""

    def next_step(self, observation_hash: str) -> CandidateStep:
        if self.stopped:
            raise WorkflowRefused("replay_stopped", self.stopped)
        if self.position >= len(self.binding.candidate.steps):
            raise WorkflowRefused("replay_complete")
        step = self.binding.candidate.steps[self.position]
        if observation_hash != step.expected_before_hash:
            self.stopped = f"drift_at_step_{step.sequence}"
            raise WorkflowRefused("replay_drift", f"step {step.sequence}: observation differs from the candidate's; stopped for repair")
        return step

    def confirm(self, step: CandidateStep, observation_after_hash: str) -> bool:
        """The step ran; did the page become what the candidate expected? False stops the replay."""
        if step is not self.binding.candidate.steps[self.position]:
            raise WorkflowRefused("replay_step_out_of_order")
        if observation_after_hash != step.expected_after_hash:
            self.stopped = f"unexpected_result_at_step_{step.sequence}"
            return False
        self.position += 1
        return True

    @property
    def complete(self) -> bool:
        return not self.stopped and self.position == len(self.binding.candidate.steps)
