"""Pure attempt-local cross-workflow lineage guard (AE04a).

Inputs are canonical workflow IDs from server-owned order/package records, not
client metadata or a history of every flow ever visited. An ancestry contains
the root through the *current* flow. Return a fresh child ancestry; never append
it back onto the invoking order/task. Repeating a node attempt therefore builds
the same child lineage rather than mistaking a legitimate retry for recursion.

No runtime, file, database, task or configuration API is imported here.
"""
from __future__ import annotations

MAX_WORKFLOW_INVOCATION_DEPTH = 32
MAX_WORKFLOW_ID_LENGTH = 256


class WorkflowInvocationRejected(RuntimeError):
    """A definite pre-launch refusal; existing invoke-node FAILED semantics fit."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _workflow_id(value: object) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= MAX_WORKFLOW_ID_LENGTH:
        raise WorkflowInvocationRejected("workflow_lineage_invalid", "Workflow lineage identity is invalid")
    normalized = value.strip()
    if (not normalized
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise WorkflowInvocationRejected("workflow_lineage_invalid", "Workflow lineage identity is invalid")
    return normalized


def advance_workflow_ancestry(
    *,
    current_workflow_id: str,
    target_workflow_id: str,
    ancestry: list[str] | tuple[str, ...] | None,
    max_depth: int,
) -> tuple[str, ...]:
    """Reject cycles or excess depth before any child order/effect is created.

    Root depth is zero; A -> B has depth one. ``max_depth`` is an explicit
    caller policy, not borrowed from the unrelated native-subagent limit.
    None/empty ancestry may bootstrap a known *root* only. The adapter must
    never use it for a legacy child whose upstream lineage is unavailable.
    """
    if type(max_depth) is not int or not 0 <= max_depth <= MAX_WORKFLOW_INVOCATION_DEPTH:
        raise WorkflowInvocationRejected("workflow_depth_policy_invalid", "Workflow invocation depth policy is invalid")
    current = _workflow_id(current_workflow_id)
    target = _workflow_id(target_workflow_id)
    if ancestry is None:
        lineage: tuple[str, ...] = (current,)
    else:
        if not isinstance(ancestry, (list, tuple)):
            raise WorkflowInvocationRejected("workflow_lineage_invalid", "Workflow ancestry must be a bounded sequence")
        # Check length before inspecting elements: arbitrary large input never
        # turns this preflight into an unbounded scan or generator consumption.
        if len(ancestry) > max_depth + 1:
            raise WorkflowInvocationRejected("workflow_invocation_depth_exceeded", "Workflow invocation depth limit reached")
        lineage = tuple(_workflow_id(item) for item in ancestry) or (current,)
    if len(set(lineage)) != len(lineage):
        raise WorkflowInvocationRejected("workflow_invocation_cycle", "Workflow ancestry already contains a cycle")
    if lineage[-1] != current:
        raise WorkflowInvocationRejected("workflow_lineage_invalid", "Workflow ancestry does not end at the invoking workflow")
    if target in lineage:
        raise WorkflowInvocationRejected("workflow_invocation_cycle", "Cross-workflow invocation cycle rejected")
    if len(lineage) > max_depth:
        raise WorkflowInvocationRejected("workflow_invocation_depth_exceeded", "Workflow invocation depth limit reached")
    return (*lineage, target)
