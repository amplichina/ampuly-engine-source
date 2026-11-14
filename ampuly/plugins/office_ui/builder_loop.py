"""Draft, try it offline, explain what broke, propose a fix, try again.

A process that has never been run is a guess written down. The loop that turns
it into a candidate is: compile the draft, run it against the offline
environment, and when it fails, say where and why in terms of the description
rather than the stack.

Four things this refuses to do, each of which is how a builder loop usually
goes wrong:

**It does not apply its own fixes.** A proposal is a proposal. Somebody has to
accept it, because a loop that edits and re-runs itself will eventually find
something that passes and nobody will know what it changed to get there.

**It does not run forever.** After a fixed number of rounds it stops and says
it has been going in circles. That is information -- a process failing five
different ways is a description problem, not a bug to grind at.

**A trial that could not run is not a trial that passed.** An error setting up
the trial leaves the round ``unrun``, which never counts toward readiness.

**Passing offline does not unlock release.** The offline environment has no
customer, no real account and no money in it. The record says ``simulated``
and the release path is required to keep asking for the rest.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from ampuly.plugins.office_ui.process_spec import (
    ProcessSpec,
    ProcessSpecError,
    compile_to_workflow,
    diff_specs,
)

MAX_ROUNDS = 5
OUTCOMES = ("passed", "failed", "unrun", "validated")


class BuilderError(ValueError):
    """The loop refuses rather than producing a candidate nobody can explain."""


class ValidationOnly(BuilderError):
    """The runner checked shape, but did not execute this process."""


@dataclass
class TrialResult:
    """One offline run of one draft, and what it established."""

    round_number: int
    spec_digest: str
    outcome: str
    failed_step: str = ""
    reason: str = ""
    detail: str = ""
    at: str = field(default_factory=lambda: datetime.now().isoformat())

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise BuilderError(f"unknown trial outcome {self.outcome!r}")

    @property
    def counts_as_evidence(self) -> bool:
        """An unrun round establishes nothing, in either direction."""
        return self.outcome in {"passed", "failed"}

    def to_payload(self) -> dict[str, Any]:
        return {
            "round": self.round_number, "spec_digest": self.spec_digest,
            "outcome": self.outcome, "failed_step": self.failed_step,
            "reason": self.reason, "detail": self.detail, "at": self.at,
            "counts_as_evidence": self.counts_as_evidence,
        }


@dataclass
class FixProposal:
    """A change somebody has to accept, tied to the trial that motivated it."""

    round_number: int
    step_id: str
    change: str
    because: str
    applied: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "round": self.round_number, "step_id": self.step_id,
            "change": self.change, "because": self.because, "applied": self.applied,
        }


def explain_failure(spec: ProcessSpec, failed_step: str, raw: str) -> tuple[str, str]:
    """Say what broke in the words of the description, not of the stack.

    Returns (reason, detail). The reason is what a person reading the process
    can act on; the raw text is kept as detail rather than thrown away, because
    the person who wrote the step is not always the person debugging it.
    """
    step = next((s for s in spec.steps if s.id == failed_step), None)
    text = str(raw or "").strip()
    if step is None:
        return ("这一步不在流程描述里，试跑跑到了描述之外的东西", text)
    lowered = text.lower()
    if lowered.startswith("question:"):
        # trial_runner.QUESTION_MARK: the step asked a person and waits. Checked first -- a step id or an attempt tag
        # containing "tool", "permission" or "approval" must not turn a question into one of the verdicts below.
        return (f"「{step.name}」向负责人提了问，等回答；回答后从这里继续", text)
    if "offline home has no tool" in lowered:
        return (f"「{step.name}」要用的工具离线环境里没有，这一步只能在真实环境试", text)
    if "names no branch" in lowered:
        return (f"「{step.name}」的答复没有说走哪条分支；可选的分支写在详情里", text)
    if "human node rejected" in lowered:
        return (f"「{step.name}」被打回了；上游要按打回意见返工，再送审", text)
    if "human gate" in lowered:
        return (f"「{step.name}」要等人决定，试跑到这里停下；后面的步骤没有跑到", text)
    if "tool" in lowered and ("not" in lowered or "denied" in lowered or "refus" in lowered):
        return (f"「{step.name}」用到了这一步没有授权的工具", text)
    if "timeout" in lowered or "timed out" in lowered:
        return (f"「{step.name}」在时限内没有做完", text)
    if "permission" in lowered or "approval" in lowered:
        return (f"「{step.name}」需要有人批准，试跑里没有人", text)
    if not step.outputs:
        return (f"「{step.name}」没有声明要交出什么，所以没人能判断它做完了", text)
    return (f"「{step.name}」没有产出它声明的 {', '.join(step.outputs)}", text)


def propose_fix(spec: ProcessSpec, trial: TrialResult) -> FixProposal | None:
    """One change that would plausibly address this failure, unapplied.

    Deliberately narrow: it proposes the thing the failure points at, and
    nothing else. A proposal that rewrites half the process to make a test go
    green is how a loop produces a candidate nobody can explain.
    """
    if trial.outcome != "failed":
        return None
    step = next((s for s in spec.steps if s.id == trial.failed_step), None)
    if step is None:
        return FixProposal(
            trial.round_number, trial.failed_step,
            change="把这一步补进流程描述，或者把引用它的地方去掉",
            because="试跑跑到了描述里没有的步骤",
        )
    if "向负责人提了问" in trial.reason:
        return None  # nothing to fix: someone has to answer
    if "离线环境里没有" in trial.reason:
        return FixProposal(
            trial.round_number, step.id,
            change="把这一步改成人工确认后在真实环境做，或者先把它从离线试跑里拿掉",
            because=trial.reason,
        )
    if "没有说走哪条分支" in trial.reason:
        return FixProposal(
            trial.round_number, step.id,
            change="放行时在答复里写上要走的分支条件原话（步骤上写的那句）",
            because=trial.reason,
        )
    if "被打回了" in trial.reason:
        return FixProposal(
            trial.round_number, step.id,
            change="按打回意见改描述或上游那一步，保存新版本后再跑一轮；打回本身不是流程的错",
            because=trial.reason,
        )
    if "要等人决定" in trial.reason:
        return FixProposal(
            trial.round_number, step.id,
            change="试跑目前到第一个人工节点为止：把人工确认挪到流程末尾，或者把它后面的步骤拆成另一条流程先试",
            because=trial.reason,
        )
    if "工具" in trial.reason:
        return FixProposal(
            trial.round_number, step.id,
            change="给这一步补上它确实需要的工具授权，或者改成不需要那件工具的做法",
            because=trial.reason,
        )
    if "有人批准" in trial.reason:
        return FixProposal(
            trial.round_number, step.id,
            change="给这一步加人工确认（谁批写清楚）",
            because=trial.reason,
        )
    if "时限" in trial.reason:
        return FixProposal(
            trial.round_number, step.id,
            change="放宽这一步的时限，或者把它拆成两步",
            because=trial.reason,
        )
    return FixProposal(
        trial.round_number, step.id,
        change="确认这一步到底该交出什么，并让它真的产出",
        because=trial.reason,
    )


@dataclass
class BuilderSession:
    """One draft's journey towards being a candidate somebody can review."""

    spec: ProcessSpec
    max_rounds: int = MAX_ROUNDS
    trials: list[TrialResult] = field(default_factory=list)
    proposals: list[FixProposal] = field(default_factory=list)
    history: list[ProcessSpec] = field(default_factory=list)
    _trial_active: bool = field(default=False, init=False, repr=False)

    @property
    def rounds_used(self) -> int:
        return len([t for t in self.trials if t.counts_as_evidence])

    @property
    def exhausted(self) -> bool:
        return len(self.trials) >= self.max_rounds

    @property
    def passed(self) -> bool:
        return (
            not self._trial_active
            and bool(self.trials)
            and self.trials[-1].outcome == "passed"
            and self.trials[-1].spec_digest == self.spec.digest()
        )

    def run_trial(self, runner: Callable[[dict[str, Any]], tuple[bool, str, str]]) -> TrialResult:
        """Synchronous validation/test compatibility; execution uses run_trial_async."""
        snapshot, number = self._begin_trial()
        try:
            try:
                answer = runner(compile_to_workflow(snapshot))
                if inspect.isawaitable(answer):
                    if inspect.iscoroutine(answer):
                        answer.close()
                    raise BuilderError("async runner requires run_trial_async")
                return self._record_trial(snapshot, number, answer=answer)
            except Exception as exc:
                return self._record_trial(snapshot, number, error=exc)
        finally:
            self._trial_active = False

    async def run_trial_async(
        self, runner: Callable[[dict[str, Any]], Awaitable[tuple[bool, str, str]] | tuple[bool, str, str]],
    ) -> TrialResult:
        """Await the existing runner, without inventing another node scheduler.

        The runner must clean up its owned execution before propagating
        cancellation. Sync runners are only for fast compile/shape checks.
        A cancelled attempt is unrun evidence, not a claim of side-effect rollback.
        """
        snapshot, number = self._begin_trial()
        try:
            try:
                answer = runner(compile_to_workflow(snapshot))
                if inspect.isawaitable(answer):
                    answer = await answer
                return self._record_trial(snapshot, number, answer=answer)
            except asyncio.CancelledError:
                self.trials.append(TrialResult(
                    number, snapshot.digest(), "unrun", reason="试跑已中断，结果未核验",
                    detail="trial_cancelled; no pass evidence; effects are not presumed rolled back",
                ))
                raise
            except Exception as exc:
                return self._record_trial(snapshot, number, error=exc)
        finally:
            self._trial_active = False

    def _begin_trial(self) -> tuple[ProcessSpec, int]:
        if self._trial_active:
            raise BuilderError("trial_already_running")
        if self.exhausted:
            raise BuilderError(
                f"this draft has used {len(self.trials)} trial attempts; "
                "that is a description problem for a person to look at, not another round"
            )
        snapshot = copy.deepcopy(self.spec)
        self._trial_active = True
        return snapshot, len(self.trials) + 1

    def _record_trial(self, snapshot: ProcessSpec, round_number: int, *,
                      answer: Any = None, error: Exception | None = None) -> TrialResult:
        digest = snapshot.digest()
        if isinstance(error, ProcessSpecError):
            result = TrialResult(
                round_number, digest, "unrun",
                reason="描述还没写完，没跑起来", detail=str(error)[:400],
            )
            self.trials.append(result)
            return result
        if isinstance(error, ValidationOnly):
            result = TrialResult(round_number, digest, "validated",
                                 reason="结构检查通过，流程尚未执行", detail=str(error)[:400])
            self.trials.append(result)
            return result
        if error is None and not (
            isinstance(answer, tuple) and len(answer) == 3
            and isinstance(answer[0], bool) and isinstance(answer[1], str)
            and isinstance(answer[2], str)
        ):
            error = BuilderError("runner must return (bool, step_id, detail)")
        if error is not None:
            result = TrialResult(
                round_number, digest, "unrun",
                reason="试跑本身没跑起来，这不说明流程对或不对",
                detail=f"{type(error).__name__}: {error}"[:400],
            )
            self.trials.append(result)
            return result
        ok, failed_step, raw = answer
        if ok:
            # Where the run stopped -- at a human node, say -- travels with the
            # pass, so a green round still says what it did not exercise.
            result = TrialResult(round_number, digest, "passed", reason="离线试跑通过", detail=str(raw or "")[:400])
            self.trials.append(result)
            return result
        reason, detail = explain_failure(snapshot, failed_step, raw)
        result = TrialResult(round_number, digest, "failed", failed_step=str(failed_step or ""),
                             reason=reason, detail=detail)
        self.trials.append(result)
        proposal = propose_fix(snapshot, result)
        if proposal is not None:
            self.proposals.append(proposal)
        return result

    def accept_proposal(self, index: int, revised: ProcessSpec) -> ProcessSpec:
        """A person accepts a proposal and supplies the revised description.

        The loop does not write the revision itself. It says what it thinks is
        wrong and who decided to change what; the change is somebody's.
        """
        if self._trial_active:
            raise BuilderError("cannot apply a revision while a trial is running")
        if not 0 <= index < len(self.proposals):
            raise BuilderError(f"there is no proposal {index}")
        proposal = self.proposals[index]
        if proposal.applied:
            raise BuilderError("that proposal has already been taken up")
        self.history.append(self.spec)
        proposal.applied = True
        self.spec = revised
        return revised

    def candidate(self) -> dict[str, Any]:
        """What a reviewer is asked to approve, with what it does not prove."""
        if not self.passed:
            raise BuilderError(
                "this draft has not passed an offline trial, so there is no candidate to review"
            )
        workflow = compile_to_workflow(self.spec)
        previous = self.history[-1] if self.history else None
        return {
            "workflow": workflow,
            "spec": self.spec.to_payload(),
            "diff": diff_specs(previous, self.spec),
            "trials": [t.to_payload() for t in self.trials],
            "proposals": [p.to_payload() for p in self.proposals],
            # The offline environment has no customer, no real account and no
            # money in it. Saying "simulated" here is what keeps a green run
            # from being read as permission to ship.
            "evidence": "simulated",
            "unlocks_release": False,
            "still_needed": [
                "真实 Provider 上跑一次",
                "人工评审这份 diff 与其中的假设",
                "客户验收标准由客户确认",
            ],
            "trace": {
                "rounds": self.rounds_used,
                "spec_digest": self.spec.digest(),
                "chain": [s.digest()[:12] for s in [*self.history, self.spec]],
            },
        }


def session_id_for(spec: ProcessSpec, actor: str) -> str:
    """A stable id for one person's attempt at one description."""
    return hashlib.sha256(f"{spec.spec_id}:{spec.digest()}:{actor}".encode("utf-8")).hexdigest()[:16]
