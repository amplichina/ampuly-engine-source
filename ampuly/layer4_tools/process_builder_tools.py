"""Tools for describing a delivery process, with no way to publish one.

The assistant can read a requirement written in the customer's own words and
propose a process from it. What it cannot do through these tools is install
anything, publish anything, or touch a process that is running -- a Builder
makes candidates, and the separation is enforced here rather than asked for in
a prompt.

The proposal tool is deliberately unhelpful in one direction: it will not
accept a description with holes and quietly fill them. It returns the holes as
questions, addressed to a person. That is the whole reason the description
exists in a form other than YAML -- so that "nobody said who reviews this"
surfaces as a question rather than as a default that ships.
"""

from __future__ import annotations

import json
from typing import Any

from ampuly.layer4_tools.file_ops import _resolve_task_path
from ampuly.layer4_tools.registry import ToolDefinition, ToolInvocationValidationError

MAX_REQUIREMENT_BYTES = 200_000


def create_process_builder_tools() -> list[ToolDefinition]:
    """Read a requirement, propose a process, and never ship one."""

    async def read_requirement(file_path: str = "", task: Any = None) -> dict[str, Any]:
        """Read one requirement document from inside the task's workspace."""
        clean = str(file_path or "").strip()
        if not clean:
            raise ToolInvocationValidationError(
                "no file was named",
                correction="Pass the path of the requirement, relative to this order's workspace.",
            )
        path = _resolve_task_path(clean, task)
        if not path.is_file():
            raise ToolInvocationValidationError(
                f"{clean} is not a file in this workspace",
                correction="List the workspace first, then read a file that is in it.",
            )
        size = path.stat().st_size
        if size > MAX_REQUIREMENT_BYTES:
            raise ToolInvocationValidationError(
                f"{clean} is {size} bytes; a requirement that large is not one document",
                correction="Point at the section that describes the process.",
            )
        return {
            "path": clean,
            "bytes": size,
            "content": path.read_text(encoding="utf-8", errors="replace"),
        }

    async def propose_process(spec_json: str = "", task: Any = None) -> dict[str, Any]:
        """Turn a description into a draft, or into the questions it still owes.

        Never installs and never publishes. What comes back is a draft plus
        the list of things a person has to answer -- filling those in here
        would put a guess where a decision belongs.
        """
        from ampuly.plugins.office_ui.process_spec import (
            ProcessSpecError,
            compile_to_workflow,
            from_payload,
            unresolved,
        )

        _ = task
        try:
            payload = json.loads(str(spec_json or "").strip() or "{}")
        except ValueError as exc:
            raise ToolInvocationValidationError(
                f"the description is not valid JSON: {exc}",
                correction="Send the process description as one JSON object.",
            ) from exc
        try:
            spec = from_payload(payload)
        except ProcessSpecError as exc:
            raise ToolInvocationValidationError(
                str(exc), correction="Fix the shape and send it again.",
            ) from exc

        review = unresolved(spec)
        if not spec.ready:
            # Not an error: an unfinished description is a normal, useful
            # thing to have. It just cannot become a runnable process yet.
            return {
                "ready": False,
                "spec_id": spec.spec_id,
                "version": spec.version,
                "digest": spec.digest(),
                "questions": review["missing"],
                "assumptions": review["assumptions"],
                "compiled": False,
                "candidate": {"spec": spec.to_payload(), "persisted": False, "requires_owner_review": True, "unlocks_release": False},
                "note": "这份描述还答不上上面这些问题，所以不能编译成可跑的流程。这不是错误，是还差人回答。",
            }
        workflow = compile_to_workflow(spec)
        return {
            "ready": True,
            "spec_id": spec.spec_id,
            "version": spec.version,
            "digest": spec.digest(),
            "questions": [],
            "assumptions": review["assumptions"],
            "compiled": True,
            "status": workflow["status"],
            "node_ids": [n["id"] for n in workflow["nodes"]],
            "candidate": {
                "id": f"{spec.spec_id}@{spec.version}:{spec.digest()}",
                "spec": spec.to_payload(),
                "workflow": workflow,
                "requires_owner_review": True,
                "unlocks_release": False,
                "persisted": False,
                "save_action": "process_spec_save",
            },
            # Said in the tool result so it cannot be lost between here and a
            # person: compiling is not publishing, and this is a draft.
            "note": "已编译为草稿。草稿不是发布：要跑它，还要人评审、试跑，并由 owner 决定。",
        }

    return [
        ToolDefinition(
            name="read_requirement",
            description=(
                "Read one requirement document from this order's workspace, so a "
                "process can be described from what the customer actually said. "
                "Reads only; the path must be inside the workspace."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path relative to this order's workspace.",
                    },
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
            func=read_requirement,
            category="process_builder",
            requires_confirmation=False,
            concurrency_safe=True,
            read_only=True,
            permission_effects=["local_read"],
        ),
        ToolDefinition(
            name="propose_process",
            description=(
                "Turn a process description into a draft workflow, or report the "
                "questions it cannot yet answer. Never installs, never publishes and "
                "never touches a running process: this produces a candidate for a "
                "person to review. An incomplete description comes back as questions "
                "rather than being completed with defaults."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "spec_json": {
                        "type": "string",
                        "description": "The process description as one JSON object.",
                    },
                },
                "required": ["spec_json"],
                "additionalProperties": False,
            },
            func=propose_process,
            category="process_builder",
            requires_confirmation=False,
            concurrency_safe=True,
            read_only=True,
            permission_effects=["runtime_internal"],
        ),
    ]
