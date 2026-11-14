"""A process described once, in the words of the business, then compiled.

The way a delivery process gets built today is that somebody writes YAML. That
puts the person who understands the work and the person who can edit the file
in different chairs, and the gap between them is where "that is not what I
meant" lives.

A ProcessSpec is the description: what starts it and what finishes it, who
does each step, what goes in and comes out, where it branches, where a person
must look, what leaves the machine, what counts as done, how long it may take
and whose data it may touch. It compiles into the workflow the runtime already
runs -- the same node model, not a second one.

The part that matters more than the compiling:

**A spec that is not finished says what is missing.** It does not compile with
defaults filled in. "No acceptance criteria" becomes a question addressed to a
person, not an empty list that later reads as "nothing to check".

**An assumption is recorded as an assumption.** When the description had to
guess -- a role, a time limit -- the guess is carried in the spec and shown in
the diff, because a guess that looks like a decision is how the wrong thing
gets built confidently.

**A Builder may only create a candidate.** It never edits a running process;
the order that is running keeps the version it started with. That separation
is in `compile_to_workflow`, which always produces a draft.
"""

from __future__ import annotations

import hashlib
import json
import re
from graphlib import CycleError, TopologicalSorter
from dataclasses import dataclass, field
from typing import Any

SPEC_CONTRACT = 1
STEP_KINDS = ("ai_do", "human_do", "human_review", "external_call", "invoke_workflow")
# A step that a person must sign off, in the vocabulary the runtime already
# uses, so nothing has to be translated at run time.
GATE_REQUIRED = "human_required"
ID = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class ProcessSpecError(ValueError):
    """A description that cannot be built from is not silently built from."""


@dataclass(frozen=True)
class Assumption:
    """Something nobody said, that had to be decided anyway."""

    about: str
    took: str
    because: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {"about": self.about, "took": self.took, "because": self.because}


@dataclass
class Step:
    """One thing that happens, and who it happens by."""

    id: str
    name: str
    kind: str = "ai_do"
    role: str = ""
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    responsibility: str = ""
    standard: list[str] = field(default_factory=list)
    human_gate: bool = False
    gate_to: str = "owner"
    branch_when: str = ""
    side_effect: str = ""
    invoke: dict[str, Any] = field(default_factory=dict)
    time_limit_hours: int = 0
    # An AI step's own model ("" = the configured default); compiled onto the node, read by the runner.
    model: str = ""

    def __post_init__(self) -> None:
        if not ID.match(str(self.id or "")):
            raise ProcessSpecError(f"{self.id!r} is not a usable step id (lowercase, letters and _)")
        for key in ("id", "name", "kind", "role", "responsibility", "gate_to", "branch_when", "side_effect", "model"):
            if not isinstance(getattr(self, key), str):
                raise ProcessSpecError(f"{self.id}: {key} must be text")
        if self.kind not in STEP_KINDS:
            raise ProcessSpecError(f"unknown step kind {self.kind!r}")
        for key in ("inputs", "outputs", "writes", "tools", "skills", "depends_on", "standard"):
            value = getattr(self, key)
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                raise ProcessSpecError(f"{self.id}: {key} must be a list of non-empty strings")
        if not isinstance(self.invoke, dict):
            raise ProcessSpecError(f"{self.id}: invoke must be an object")
        if not isinstance(self.human_gate, bool):
            raise ProcessSpecError(f"{self.id}: human_gate must be a boolean")
        if isinstance(self.time_limit_hours, bool) or not isinstance(self.time_limit_hours, int) or self.time_limit_hours < 0:
            raise ProcessSpecError(f"{self.id}: time_limit_hours must be a non-negative integer")

    def missing(self) -> list[str]:
        """What a person still has to answer about this step."""
        gaps: list[str] = []
        if not str(self.name or "").strip():
            gaps.append(f"{self.id}: 这一步叫什么")
        if self.kind in {"ai_do", "external_call", "invoke_workflow"} and not str(self.role or "").strip():
            gaps.append(f"{self.id}: 谁来做")
        if self.kind not in {"invoke_workflow", "external_call"} and not self.outputs:
            gaps.append(f"{self.id}: 做完交出什么")
        if self.kind == "external_call" and not self.outputs and not self.tools:
            gaps.append(f"{self.id}: 对外步骤要调用什么组件或交出什么")
        if not self.standard:
            gaps.append(f"{self.id}: 做成什么样算做完")
        if self.kind in {"human_do", "human_review"} and not str(self.gate_to or "").strip():
            gaps.append(f"{self.id}: 谁来看")
        if self.kind == "invoke_workflow" and not str(self.invoke.get("workflow", "") or "").strip():
            gaps.append(f"{self.id}: 要调用哪条工作流")
        if self.side_effect and not self.human_gate:
            # Something leaves the machine here and nobody was asked.
            gaps.append(f"{self.id}: 这一步会对外产生动作（{self.side_effect}），谁批")
        return gaps

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "kind": self.kind, "role": self.role,
            "inputs": list(self.inputs), "outputs": list(self.outputs),
            "writes": list(self.writes), "tools": list(self.tools), "skills": list(self.skills),
            "depends_on": list(self.depends_on), "responsibility": self.responsibility,
            "standard": list(self.standard), "human_gate": self.human_gate, "gate_to": self.gate_to,
            "branch_when": self.branch_when, "side_effect": self.side_effect,
            "invoke": dict(self.invoke),
            "time_limit_hours": self.time_limit_hours,
            **({"model": self.model} if self.model else {}),
        }


@dataclass
class ProcessSpec:
    """The whole description, and what it still cannot answer."""

    spec_id: str
    name: str
    version: str = "0.1.0"
    trigger: str = ""
    finishes_when: str = ""
    intake_fields: list[dict[str, Any]] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    data_scope: str = ""
    time_limit_hours: int = 0
    assumptions: list[Assumption] = field(default_factory=list)
    author: str = ""
    contract: int = SPEC_CONTRACT

    def __post_init__(self) -> None:
        for key in ("spec_id", "name", "version", "trigger", "finishes_when", "data_scope", "author"):
            if not isinstance(getattr(self, key), str):
                raise ProcessSpecError(f"{key} must be text")
        if isinstance(self.time_limit_hours, bool) or not isinstance(self.time_limit_hours, int) or self.time_limit_hours < 0:
            raise ProcessSpecError("time_limit_hours must be a non-negative integer")
        if not ID.match(str(self.spec_id or "")):
            raise ProcessSpecError(f"{self.spec_id!r} is not a usable process id")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", self.version):
            raise ProcessSpecError("version must name a semantic candidate version")
        if any(not isinstance(item, str) or not item.strip() for item in self.acceptance_criteria):
            raise ProcessSpecError("acceptance_criteria must contain non-empty strings")
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ProcessSpecError(f"two steps share the id {step.id!r}")
            seen.add(step.id)
        for step in self.steps:
            for parent in step.depends_on:
                if parent not in seen:
                    raise ProcessSpecError(f"{step.id!r} depends on {parent!r}, which is not a step here")
        try:
            tuple(TopologicalSorter({step.id: step.depends_on for step in self.steps}).static_order())
        except CycleError as exc:
            raise ProcessSpecError("step dependencies contain a cycle") from exc

    def missing(self) -> list[str]:
        """Everything a person must still answer, named in their words.

        Returned rather than defaulted. A process compiled over a gap runs
        confidently in the wrong direction, and the gap is only found when a
        customer reads the delivery.
        """
        gaps: list[str] = []
        if not str(self.trigger or "").strip():
            gaps.append("什么时候开始这条流程")
        if not str(self.finishes_when or "").strip():
            gaps.append("到什么程度算这单结束")
        if not self.steps:
            gaps.append("这条流程有哪些步骤")
        if not self.acceptance_criteria:
            gaps.append("客户凭什么说这单做完了（验收标准）")
        if not str(self.data_scope or "").strip():
            gaps.append("这条流程能碰哪些数据")
        if not any(step.human_gate or step.kind in {"human_do", "human_review"} for step in self.steps) and self.steps:
            gaps.append("整条流程没有任何人工确认，确认这是有意的")
        by_id = {s.id: s for s in self.steps}
        for step in self.steps:
            if str(step.branch_when or "").strip():
                deciders = [
                    d for d in step.depends_on
                    if d in by_id and by_id[d].kind in {"human_do", "human_review"}
                ]
                if not deciders:
                    # A branch is a decision. The only decider this product has is
                    # a person at a human node; a condition nobody evaluates is a
                    # step that runs regardless, which is not a branch.
                    gaps.append(f"{step.id}: 这一步有分支条件「{step.branch_when}」，谁来判断走不走？分支步骤要依赖一个人工确认步骤")
        for step in self.steps:
            gaps.extend(step.missing())
        return gaps

    @property
    def ready(self) -> bool:
        return not self.missing()

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_payload(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "spec_id": self.spec_id,
            "name": self.name,
            "version": self.version,
            "trigger": self.trigger,
            "finishes_when": self.finishes_when,
            "intake_fields": [dict(f) for f in self.intake_fields],
            "steps": [s.to_payload() for s in self.steps],
            "acceptance_criteria": list(self.acceptance_criteria),
            "data_scope": self.data_scope,
            "time_limit_hours": self.time_limit_hours,
            "assumptions": [a.to_payload() for a in self.assumptions],
            "author": self.author,
        }


def from_payload(payload: dict[str, Any]) -> ProcessSpec:
    """Validate untrusted assistant/UI data before constructing a candidate."""
    if not isinstance(payload, dict):
        raise ProcessSpecError("process description must be an object")
    for key in ("steps", "assumptions", "intake_fields", "acceptance_criteria"):
        if key in payload and not isinstance(payload[key], list):
            raise ProcessSpecError(f"{key} must be a list")
    for key in ("spec_id", "name", "version", "trigger", "finishes_when", "data_scope", "author"):
        if key in payload and not isinstance(payload[key], str):
            raise ProcessSpecError(f"{key} must be text")
    if any(not isinstance(value, str) or not value.strip() for value in payload.get("acceptance_criteria", [])):
        raise ProcessSpecError("acceptance_criteria must contain non-empty strings")
    for key in ("contract", "time_limit_hours"):
        if key in payload and (isinstance(payload[key], bool) or not isinstance(payload[key], int) or payload[key] < 0):
            raise ProcessSpecError(f"{key} must be a non-negative integer")
    try:
        return _from_payload(payload)
    except (TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, ProcessSpecError):
            raise
        raise ProcessSpecError("invalid process description field types") from exc


def _from_payload(payload: dict[str, Any]) -> ProcessSpec:
    data = dict(payload or {})
    contract = int(data.get("contract", 0) or 0)
    if contract != SPEC_CONTRACT:
        raise ProcessSpecError(
            f"this spec is contract {contract}, and this build reads {SPEC_CONTRACT}"
        )
    steps = [Step(**{k: v for k, v in dict(item).items()}) for item in list(data.get("steps", []) or [])]
    assumptions = [Assumption(**dict(item)) for item in list(data.get("assumptions", []) or [])]
    return ProcessSpec(
        spec_id=str(data.get("spec_id", "")),
        name=str(data.get("name", "")),
        version=str(data.get("version", "0.1.0")),
        trigger=str(data.get("trigger", "")),
        finishes_when=str(data.get("finishes_when", "")),
        intake_fields=[dict(f) for f in list(data.get("intake_fields", []) or [])],
        steps=steps,
        acceptance_criteria=[str(c) for c in list(data.get("acceptance_criteria", []) or [])],
        data_scope=str(data.get("data_scope", "")),
        time_limit_hours=int(data.get("time_limit_hours", 0) or 0),
        assumptions=assumptions,
        author=str(data.get("author", "")),
    )


def compile_to_workflow(spec: ProcessSpec, *, org_id: str = "") -> dict[str, Any]:
    """Turn a finished description into a workflow draft the runtime can run.

    Always a draft. A Builder creates candidates; it does not edit what is
    running, because an order that started on one version has to finish on it.

    Refuses an unfinished spec rather than filling the holes. The refusal
    carries the questions, so the answer is "go and ask these five things",
    not "it failed".
    """
    gaps = spec.missing()
    if gaps:
        raise ProcessSpecError(
            "this description is not finished; it cannot be compiled into a runnable "
            "process until somebody answers: " + "；".join(gaps)
        )
    nodes: list[dict[str, Any]] = []
    for step in spec.steps:
        node: dict[str, Any] = {
            "id": step.id,
            "name": step.name,
            # Outbound work uses the existing AI executor and its Effect
            # Gateway tools, never a fictional external_call runtime kind.
            "type": "ai_do" if step.kind == "external_call" else step.kind,
            "responsibility": step.responsibility or step.name,
            "standard": list(step.standard),
            "deliverables": list(step.outputs),
        }
        if step.role:
            node["role"] = step.role
        if step.kind in {"human_do", "human_review"}:
            node["role"] = step.role or step.kind
            node["assignee"] = step.gate_to or "owner"
        if step.inputs:
            node["inputs"] = list(step.inputs)
        if step.side_effect:
            node["side_effect"] = step.side_effect
        if step.kind == "invoke_workflow":
            node["invoke"] = dict(step.invoke)
        if step.time_limit_hours:
            node["time_limit_hours"] = step.time_limit_hours
        if step.tools:
            node["tools"] = list(step.tools)
        if step.skills:
            node["skills"] = list(step.skills)
        if step.writes:
            node["writes"] = list(step.writes)
        if step.depends_on:
            node["depends_on"] = list(step.depends_on)
        if step.human_gate:
            node["gate"] = GATE_REQUIRED
            node["gate_to"] = step.gate_to or "owner"
        if step.branch_when:
            node["branch_when"] = step.branch_when
        if step.model:
            node["model"] = step.model
        nodes.append(node)
    return {
        "schema_version": 1,
        "id": spec.spec_id,
        "name": spec.name,
        "version": spec.version,
        # A compiled process starts as a draft, always. Nothing here can
        # publish, and nothing here touches a running order.
        "status": "draft",
        "summary": spec.trigger,
        "org_id": org_id,
        "nodes": nodes,
        "intake": {"fields": [dict(f) for f in spec.intake_fields]},
        "acceptance": {"criteria": list(spec.acceptance_criteria)},
        "process_constraints": {
            "finishes_when": spec.finishes_when, "data_scope": spec.data_scope,
            "time_limit_hours": spec.time_limit_hours,
        },
        "provenance": {
            "built_from": "process_spec",
            "spec_id": spec.spec_id,
            "spec_version": spec.version,
            "spec_digest": spec.digest(),
            "author": spec.author,
            # Carried into the draft on purpose: a reviewer reading the
            # workflow should see what nobody actually said.
            "assumptions": [a.to_payload() for a in spec.assumptions],
        },
    }


def diff_specs(before: ProcessSpec | None, after: ProcessSpec) -> dict[str, Any]:
    """What changed between two descriptions, in the terms a person reads.

    A step added or removed is the news; a step whose acceptance standard
    moved is the news that gets missed, so it is reported separately from
    everything else that changed about that step.
    """
    if before is None:
        return {
            "new": True,
            "added_steps": [s.id for s in after.steps],
            "removed_steps": [], "changed_steps": [], "standard_changed": [],
            "acceptance_changed": bool(after.acceptance_criteria),
            "assumptions": [a.to_payload() for a in after.assumptions],
        }
    left = {s.id: s for s in before.steps}
    right = {s.id: s for s in after.steps}
    changed: list[str] = []
    standard_changed: list[str] = []
    for step_id in sorted(set(left) & set(right)):
        if left[step_id].standard != right[step_id].standard:
            standard_changed.append(step_id)
        if left[step_id].to_payload() != right[step_id].to_payload():
            changed.append(step_id)
    return {
        "new": False,
        "added_steps": sorted(set(right) - set(left)),
        "removed_steps": sorted(set(left) - set(right)),
        "changed_steps": changed,
        "standard_changed": standard_changed,
        "acceptance_changed": before.acceptance_criteria != after.acceptance_criteria,
        "assumptions": [a.to_payload() for a in after.assumptions],
    }


def worker_suggestion(step_id: str, suggestion: str, *, by: str) -> dict[str, Any]:
    """A Worker's improvement, which is a note and never an edit.

    The order that is running keeps the version it started with. A Worker that
    could change the process mid-run would make "which version produced this"
    unanswerable for the delivery it is producing right now.
    """
    if not str(suggestion or "").strip():
        raise ProcessSpecError("an empty suggestion says nothing")
    if not str(by or "").strip():
        raise ProcessSpecError("a suggestion needs somebody behind it")
    return {
        "kind": "worker_suggestion",
        "step_id": str(step_id or ""),
        "suggestion": str(suggestion).strip(),
        "by": str(by).strip(),
        "applies_to": "a future candidate",
        "changes_running_process": False,
    }


def unresolved(spec: ProcessSpec) -> dict[str, Any]:
    """Everything a reviewer should see before approving a candidate."""
    return {
        "missing": spec.missing(),
        "assumptions": [a.to_payload() for a in spec.assumptions],
        "ready": spec.ready,
    }
