"""The five tools only a build job has (总控施工模式, policy, B1), bound to one running job.

Each is a thin call into the job (``master_build.JobBinding``); what it may reach is decided there and declared here:

- ``build_line_overview`` reads the line as it is live -- its manifest, steps, skills, test records and how this
  project's recent orders on it ended. A read, and the only one outside the draft.
- ``build_check_draft`` runs the deterministic checks on the draft: package problems, the builder preflight and the
  skill contract tests. A read of the draft.
- ``build_offline_trial`` puts the whole draft through the product's own offline runner (``trial_runner``), in a home
  of its own with file tools only and outward tools denied, its model calls on the job's budget. Simulated evidence.
- ``build_progress`` posts one line into the master conversation (at most one a minute, 300 characters).
- ``build_deliver`` lists the draft's changed files against the start, records the hand-over and ends the turn.

None of them sends, pays, publishes or deletes, and none reaches the network: each is declared ``runtime_internal``
or ``local_read`` so the permission layer reasons about what it really does.
"""

from __future__ import annotations

from typing import Any

from ampuly.layer4_tools.registry import COMPANY_EFFECT_RUNTIME_INTERNAL, ToolDefinition


def create_build_tools(binding: Any) -> list[ToolDefinition]:
    """The build tools, bound to ``binding`` (one job's running turn)."""

    async def build_line_overview() -> dict[str, Any]:
        return await binding.line_overview()

    async def build_check_draft() -> dict[str, Any]:
        return await binding.check_draft()

    async def build_offline_trial() -> dict[str, Any]:
        return await binding.offline_trial()

    async def build_progress(text: str) -> dict[str, Any]:
        return await binding.progress(str(text or ""))

    async def build_deliver(
        summary: str, open_questions: list[str] | None = None, tools_to_develop: list[str] | None = None,
    ) -> dict[str, Any]:
        return await binding.deliver(
            str(summary or ""),
            [str(item) for item in list(open_questions or []) if str(item or "").strip()],
            [str(item) for item in list(tools_to_develop or []) if str(item or "").strip()],
        )

    def tool(name: str, description: str, parameters: dict[str, Any], func: Any, *, effects: tuple[str, ...],
             read_only: bool) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            func=func,
            category="build",
            requires_confirmation=False,
            read_only=read_only,
            company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
            permission_effects=list(effects),
        )

    no_arguments = {"type": "object", "properties": {}}
    strings = {"type": "array", "items": {"type": "string"}}
    return [
        tool(
            "build_line_overview",
            "The business line this build started from, as it is live: name, version, status, its steps in words, its "
            "skills with versions, its latest test records, and how this project's recent orders on it ended. Read only; "
            "for a new line it says there is none.",
            no_arguments, build_line_overview, effects=("local_read",), read_only=True,
        ),
        tool(
            "build_check_draft",
            "Check the draft as it is on disk now: package problems, the builder preflight (blockers and warnings) and "
            "every skill's contract tests, with a summary in words. Deterministic; no model call.",
            no_arguments, build_check_draft, effects=("local_read",), read_only=True,
        ),
        tool(
            "build_offline_trial",
            "Run the whole draft offline through the product's own trial runner, in an isolated home with file tools "
            "only; outward tools are denied. Its model calls count against this build's cap. The result is simulated "
            "evidence, never a real test.",
            no_arguments, build_offline_trial, effects=("runtime_internal",), read_only=False,
        ),
        tool(
            "build_progress",
            "Tell the owner in the conversation, in one plain Chinese sentence, where the build is (a milestone, not "
            "every step). At most one a minute; longer text is cut to 300 characters.",
            {"type": "object", "properties": {"text": {"type": "string", "description": "One plain sentence."}},
             "required": ["text"]},
            build_progress, effects=("runtime_internal",), read_only=False,
        ),
        tool(
            "build_deliver",
            "Hand over the candidate: the draft's changed files are listed against the start, the owner is told what "
            "changed, what they still have to decide and which code tools a developer has to build, and this turn "
            "ends. Update BUILD-NOTES.md first.",
            {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "What this build changed and why, in plain Chinese."},
                    "open_questions": {**strings, "description": "What the owner still has to decide, each in one sentence."},
                    "tools_to_develop": {**strings, "description": "Code tools that would need the engine's source; not written by the build."},
                },
                "required": ["summary"],
            },
            build_deliver, effects=("runtime_internal",), read_only=False,
        ),
    ]
