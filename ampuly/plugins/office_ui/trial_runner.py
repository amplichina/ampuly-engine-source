"""Try a compiled draft on the product's own scheduler, in a home of its own.

The page trial used to stop at a shape check and say so. This is the run.
It builds a separate home for the attempt -- its own task store, harness
registry, memory and workspace under ``<ampuly_home>/builder_trials`` -- and
puts the draft through the same ``CompanyWorkItemExecutor`` and
``NativeRuntimeV2`` the product runs orders on. The only thing borrowed from
the live engine is the model. Nothing in here schedules: the dependency
order, the parking at a human node and the controller lease belong to the
executor, which is the point. A for-loop that walks the steps would prove
nothing about how the product runs them.

The offline home has no customer, no real account and no money in it, and
the runner keeps to that:

- a step that names a tool the offline home does not have is refused before
  a model is asked anything, and the trial fails at that step saying so;
- a human node parks the run, the way it does in production. When nothing
  is behind it the trial passes and says where it stopped. When steps are
  behind it they were not tried, and the trial fails at the gate rather
  than pretend;
- a parked attempt can be continued once a person has decided. The
  continuation is a fresh run in the same home over the steps that have not
  finished, with the decision handed to the human node the way a console
  answer is: ``打回`` fails it closed and the steps behind it stay untried,
  anything else releases it. Steps that already delivered are not re-run;
  their files are checked again;
- a branch is chosen by the person at the human node it hangs off: the
  reply that releases the node names the branch (the step's own
  ``branch_when`` words); the other branches and whatever depends only on
  them are skipped and recorded as skipped, never run and never called
  tried. A reply that names no branch stops there and says which words it
  could have used;
- every file a step promised has to be in the workspace afterwards, or the
  trial fails at that step;
- passing is still ``simulated`` evidence. The candidate keeps saying so.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from pathlib import Path
from typing import Any

from ampuly.core.config import AmpulyConfig, RoleConfig
from ampuly.core.events import EventBus
from ampuly.core.models import (
    DelegationRun,
    DelegationWorkItem,
    ExecutionCheckpoint,
    Phase,
    Task,
    TaskStatus,
)
from ampuly.plugins.office_ui.order_folder import folder_of
from ampuly.plugins.office_ui.order_folder import match as match_deliverable

# The whole offline tool set. File tools are fenced to the attempt's
# workspace by the task's workspace_root; nothing here sends, pays or
# generates. That is what "offline" means.
OFFLINE_TOOLS: tuple[str, ...] = (
    "file_read", "file_write", "file_edit", "apply_patch", "grep", "glob", "file_search", "list_dir",
)
HUMAN_NODE_TYPES = frozenset({"human_review", "human_do"})
# How a round's detail begins when an AI step asked a person and waits: a wait, not a failure.
QUESTION_MARK = "question:"
# Phases a work item sits in before anybody has picked it up.
_NOT_STARTED = frozenset({Phase.QUEUED, Phase.READY, Phase.READY_FOR_REWORK, Phase.WAITING_DEPENDENCIES})

DEFAULT_STEP_TIMEOUT_SEC = 90
DEFAULT_MAX_ITERATIONS = 8

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def workflow_digest(workflow: dict[str, Any]) -> str:
    raw = json.dumps(workflow, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def _attempt_base(ampuly_home: Path, workflow: dict[str, Any]) -> Path:
    spec_id = _SAFE.sub("_", str(workflow.get("id") or "draft")) or "draft"
    # Eight hex characters of the digest: the attempt home is the root of a
    # workspace whose comms inbox files already run long, and Windows stops
    # at 260 characters without saying which segment to blame.
    return Path(ampuly_home) / "builder_trials" / spec_id / workflow_digest(workflow)[:8]


def attempt_home(ampuly_home: Path, workflow: dict[str, Any], number: int) -> Path:
    """Where attempt ``number`` of this exact compiled draft lives (or would)."""
    return _attempt_base(ampuly_home, workflow) / f"attempt-{int(number)}"


def next_attempt_home(ampuly_home: Path, workflow: dict[str, Any]) -> Path:
    """A fresh directory for this attempt; earlier attempts stay as evidence.

    Attempts on one description are serialised by the Builder round claim in
    the business store, so counting siblings is enough here. A collision is
    an error, not something to paper over.
    """
    base = _attempt_base(ampuly_home, workflow)
    base.mkdir(parents=True, exist_ok=True)
    number = 1 + sum(1 for p in base.iterdir() if p.is_dir() and p.name.startswith("attempt-"))
    home = base / f"attempt-{number}"
    home.mkdir()
    (home / "workspace").mkdir()
    return home


def refuse_offline(
    workflow: dict[str, Any], *, supports_invoke_workflow: bool = False,
    available_tools: frozenset[str] | set[str] = frozenset(),
) -> tuple[bool, str, str] | None:
    """What cannot be tried offline is refused before a model is asked anything."""
    for node in list(workflow.get("nodes") or []):
        node_id = str(node.get("id", "") or "")
        if str(node.get("type", "") or "") in HUMAN_NODE_TYPES:
            continue
        if str(node.get("type", "") or "") == "invoke_workflow":
            target = str(dict(node.get("invoke", {}) or {}).get("workflow", "") or "").strip()
            if not supports_invoke_workflow:
                return (False, node_id, f"offline home has no workflow launcher for {target or 'an unnamed workflow'}")
            continue
        admitted = set(OFFLINE_TOOLS) | {str(name) for name in available_tools}
        foreign = [str(t) for t in list(node.get("tools") or []) if str(t) not in admitted]
        if foreign:
            return (
                False, node_id,
                f"offline home has no tool {', '.join(foreign)}; "
                "this step can only be tried where that tool exists",
            )
        if not list(node.get("deliverables") or []) and not list(node.get("tools") or []):
            return (False, node_id, "the step promises nothing to deliver")
    return None


def _is_human(node: dict[str, Any]) -> bool:
    return str(node.get("type", "") or "") in HUMAN_NODE_TYPES


def _role_of(node: dict[str, Any]) -> str:
    role = str(node.get("role", "") or "").strip()
    if role:
        return role
    return "human_review" if _is_human(node) else "worker"


def _inside(workspace: Path, relative: str) -> Path | None:
    """The deliverable's path, or None when it points outside the workspace."""
    try:
        candidate = (workspace / relative).resolve()
        root = workspace.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate == root or root not in candidate.parents:
        return None
    return candidate


class CompanyTrialRunner:
    """What the Builder loop calls with a compiled workflow.

    Returns the loop's ``(ok, failed_step, detail)`` triple. Every attempt
    gets its own home; the model is the engine's own, so a trial costs what
    a model turn costs and nothing else.
    """

    def __init__(
        self, *, llm: Any, ampuly_home: Path,
        step_timeout_sec: int = DEFAULT_STEP_TIMEOUT_SEC,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        budget_context: dict[str, str] | None = None,
        node_invoke_callback: Any | None = None,
        extra_tool_factory: Any | None = None,
        extra_tool_names: tuple[str, ...] | list[str] = (),
        workspace_seed: Any | None = None,
        tool_argument_checks: dict[str, Any] | None = None,
        tools_as_declared: bool = False,
    ) -> None:
        if llm is None:
            raise ValueError("an offline trial needs the engine's model; without one there is no run")
        self.llm = llm
        self.ampuly_home = Path(ampuly_home)
        self.step_timeout_sec = int(step_timeout_sec)
        self.max_iterations = int(max_iterations)
        self.budget_context = {
            str(key): str(value or "")
            for key, value in dict(budget_context or {}).items()
            if key in {"budget_id", "project_id", "customer_id", "environment_id", "order_id"}
        }
        self.node_invoke_callback = node_invoke_callback
        self.extra_tool_factory = extra_tool_factory
        self.extra_tool_names = tuple(dict.fromkeys(str(name) for name in extra_tool_names if str(name)))
        if self.budget_context.get("budget_id"):
            self.budget_context["ampuly_home"] = str(self.ampuly_home.resolve())
        # Called with the fresh, empty workspace before anything runs in it. What it puts there is the caller's
        # to account for; a seed that fails is a failed attempt, not a run on whatever did get copied.
        self.workspace_seed = workspace_seed
        # Per extra tool: a check of its arguments (returns why not, or ""). The extra tools a caller declares are the
        # only ones that may reach outside; see trial_permissions.
        self.tool_argument_checks = {str(k): v for k, v in dict(tool_argument_checks or {}).items()}
        # A node trial's steps carry the exact tools the step would hold in production (its role's own plus what its
        # nodes grant), so a step is given those and nothing more. A description being tried offline has no role
        # behind it and keeps the whole offline file set.
        self.tools_as_declared = bool(tools_as_declared)
        self.last_home: Path | None = None

    async def __call__(self, workflow: dict[str, Any]) -> tuple[bool, str, str]:
        refused = refuse_offline(
            workflow, supports_invoke_workflow=callable(self.node_invoke_callback),
            available_tools=set(self.extra_tool_names),
        )
        if refused is not None:
            return refused
        home = next_attempt_home(self.ampuly_home, workflow)
        self.last_home = home
        if callable(self.workspace_seed):
            try:
                self.workspace_seed(home / "workspace")
            except Exception as exc:
                first = str(next(iter(list(workflow.get("nodes") or [])), {}).get("id", "") or "")
                return (False, first, f"the upstream results could not be placed in the trial workspace: {exc}")
        return await _Attempt(
            workflow, home, llm=self.llm,
            step_timeout_sec=self.step_timeout_sec, max_iterations=self.max_iterations,
            budget_context=self.budget_context,
            node_invoke_callback=self.node_invoke_callback,
            extra_tool_factory=self.extra_tool_factory,
            extra_tool_names=self.extra_tool_names,
            tool_argument_checks=self.tool_argument_checks,
            tools_as_declared=self.tools_as_declared,
        ).run()


class ResumeTrialRunner(CompanyTrialRunner):
    """Continue a parked attempt after a person decided at its human node.

    The decision text is handed to the node the way a console answer is:
    a reply starting with 打回 / 驳回 / reject fails the node closed, anything
    else releases it. The attempt has to be one of this exact compiled draft;
    a description that changed since gets a fresh trial, not a continuation.
    """

    def __init__(self, *, attempt: int, reply: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.attempt = int(attempt)
        self.reply = str(reply or "").strip()
        if not self.reply:
            raise ValueError("continuing a parked attempt needs the person's decision, in words")

    async def __call__(self, workflow: dict[str, Any]) -> tuple[bool, str, str]:
        home = attempt_home(self.ampuly_home, workflow, self.attempt)
        self.last_home = home
        if not (home / "trial.json").exists():
            raise RuntimeError(
                f"attempt-{self.attempt} of this exact description version does not exist; "
                "if the description changed, run a fresh trial"
            )
        return await _Attempt(
            workflow, home, llm=self.llm,
            step_timeout_sec=self.step_timeout_sec, max_iterations=self.max_iterations,
            budget_context=self.budget_context,
            node_invoke_callback=self.node_invoke_callback,
            extra_tool_factory=self.extra_tool_factory,
            extra_tool_names=self.extra_tool_names,
            tool_argument_checks=self.tool_argument_checks,
            tools_as_declared=self.tools_as_declared,
        ).resume(self.reply)


class _Environment:
    """Everything one run needs, and how to put it away."""

    def __init__(self) -> None:
        self.store: Any = None
        self.executor: Any = None
        self.harness: Any = None
        self.sessions: list[str] = []

    async def close(self) -> None:
        from ampuly.layer2_organization.phase_hooks import unregister_dispatcher_wake

        if self.executor is not None:
            try:
                unregister_dispatcher_wake(self.executor._signal_dispatcher_wake)
            except Exception:
                pass
            pending = getattr(self.executor, "_kanban_broadcast_task", None)
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        if self.harness is not None:
            for sid in self.sessions:
                try:
                    await self.harness.close_session(sid)
                except Exception:
                    # A turn that was cut short leaves its session where it
                    # is; the registry file in this home says so.
                    pass
        if self.store is not None:
            await self.store.close()


class _Attempt:
    """One run -- or one continuation -- of one compiled draft in one home."""

    def __init__(self, workflow: dict[str, Any], home: Path, *, llm: Any,
                 step_timeout_sec: int, max_iterations: int,
                 budget_context: dict[str, str] | None = None,
                 node_invoke_callback: Any | None = None,
                 extra_tool_factory: Any | None = None,
                 extra_tool_names: tuple[str, ...] | list[str] = (),
                 tool_argument_checks: dict[str, Any] | None = None,
                 tools_as_declared: bool = False) -> None:
        self.workflow = workflow
        self.tools_as_declared = bool(tools_as_declared)
        self.home = home
        self.workspace = home / "workspace"
        self.llm = llm
        self.step_timeout_sec = step_timeout_sec
        self.max_iterations = max_iterations
        self.budget_context = dict(budget_context or {})
        # A trial gets a model plan of its own, frozen at trial start (routing phase 1, E6.9). A trial dispatched
        # under an order is ordinary order work, and order work with no plan is refused -- so a trial that runs
        # on an order's budget has to carry one, and it must not be rebuilt mid-trial or one trial would span
        # two plans.
        if "model_plan" not in self.budget_context:
            from ampuly.plugins.office_ui.order_model_plan import build_plan

            config = getattr(getattr(llm, "config", None), "llm", None) or getattr(llm, "config", None)
            if config is not None:
                self.budget_context["model_plan"] = build_plan(config, source="trial")
        self.node_invoke_callback = node_invoke_callback
        self.extra_tool_factory = extra_tool_factory
        self.extra_tool_names = tuple(dict.fromkeys(str(name) for name in extra_tool_names if str(name)))
        self.tool_argument_checks = dict(tool_argument_checks or {})
        self.nodes: list[dict[str, Any]] = [dict(n) for n in list(workflow.get("nodes") or [])]
        self.spec_id = _SAFE.sub("_", str(workflow.get("id") or "draft")) or "draft"
        self.runtime_project_id = str(self.budget_context.get("project_id") or self.spec_id)
        self.runtime_customer_id = str(self.budget_context.get("customer_id", "")) if self.budget_context else "offline"
        self.runtime_environment_id = str(self.budget_context.get("environment_id", "")) if self.budget_context else "offline"
        # Short on purpose: it names the comms session directory inside the
        # workspace, and the spec id already names the home above it.
        self.run_id = f"t-{workflow_digest(workflow)[:8]}-{home.name.rsplit('-', 1)[-1]}"
        self.final_role = _role_of(self.nodes[-1]) if self.nodes else "worker"
        # What this run schedules. A continuation covers only the steps that
        # have not finished; the rest are carried as recorded facts.
        self.run_nodes: list[dict[str, Any]] = list(self.nodes)
        self.item_ids = {str(n.get("id")): f"{self.spec_id}-{n.get('id')}" for n in self.nodes}
        self.prior_phases: dict[str, str] = {}
        self.prior_node_receipts: dict[str, dict[str, Any]] = {}
        self.skipped: dict[str, str] = {}
        # Per step: the model its calls were sent with, and whether the step named it or the default applied.
        self.model_routes: dict[str, dict[str, str]] = {}
        self.continuation = 0
        self.checkpoints: list[dict[str, Any]] = []
        # What an AI step asked a person (its request_user_input call), by node. The turn ends there; the attempt
        # records the question and parks, exactly as it does at a human gate, and a continuation carries the answer.
        self.questions: dict[str, dict[str, Any]] = {}
        # Every question this attempt has had answered, oldest first: a continuation is a fresh model turn, and a
        # step that asks twice must still see what it was told the first time.
        self.answered: list[dict[str, Any]] = []
        # A declared write scope is part of the workspace contract. Prepare
        # only its directories; never create a promised deliverable, because
        # file existence is what the trial later verifies. A deliverable's
        # folder is the part of it before any wildcard (``board/*.png`` ->
        # ``board/``); a ``dir/`` deliverable's folder is made too, and counts
        # only once a file is in it (C5-1).
        for node in self.nodes:
            candidates = [str(item) for item in list(node.get("writes") or [])]
            candidates.extend(folder_of(str(item)) for item in list(node.get("deliverables") or []))
            for relative in candidates:
                if not relative:
                    continue
                target = _inside(self.workspace, relative)
                if target is None:
                    continue
                directory = target if relative.replace("\\", "/").endswith("/") else target.parent
                directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ run
    async def run(self) -> tuple[bool, str, str]:
        if not self.nodes:
            return (False, "", "the draft has no steps")
        env = await self._environment()
        try:
            tasks = await self._seed(env.store)
            await env.executor.execute(self._plan(), tasks)
            return await self._judge(env.store)
        finally:
            await env.close()

    async def resume(self, reply: str) -> tuple[bool, str, str]:
        """A fresh run over the unfinished steps, the decision handed to the parked node.

        Re-entering the first run would mean taking its controller lease over,
        which is a recovery path with its own envelope rules; a continuation
        is simpler and says exactly what it is. Finished steps are not re-run
        -- their deliverables are in the workspace and are checked again.
        """
        if not self.nodes:
            return (False, "", "the draft has no steps")
        record = json.loads((self.home / "trial.json").read_text(encoding="utf-8"))
        parked = [str(p) for p in list(record.get("parked") or [])]
        asked = {str(k): dict(v) for k, v in dict(record.get("asked") or {}).items() if isinstance(v, dict)}
        if not parked and not asked:
            raise RuntimeError(f"nothing is parked in {self.home.name}; there is nothing to continue")
        if not parked:
            return await self._continue_with_answer(record, next(iter(asked)), asked, reply)
        phases = {str(k): str(v) for k, v in dict(record.get("phases") or {}).items()}
        self.prior_node_receipts = {
            str(key): dict(value)
            for key, value in dict(record.get("node_receipts") or {}).items()
            if isinstance(value, dict)
        }
        finished = {nid for nid, phase in phases.items() if phase == Phase.APPROVED.value}
        self.skipped = {str(k): str(v) for k, v in dict(record.get("skipped") or {}).items()}
        refused = self._choose_branches(parked[0], reply)
        if refused is not None:
            return refused
        self.continuation = int(record.get("continuation", 0) or 0) + 1
        self.run_id = f"{self.run_id}-c{self.continuation}"
        out_of_run = finished | set(self.skipped)
        self.run_nodes = [n for n in self.nodes if str(n.get("id")) not in out_of_run]
        if not self.run_nodes:
            raise RuntimeError(f"every step of {self.home.name} already ran; there is nothing to continue")
        self.item_ids = {str(n.get("id")): f"{self.spec_id}-{n.get('id')}-c{self.continuation}" for n in self.run_nodes}
        self.prior_phases = {nid: phases[nid] for nid in finished}
        env = await self._environment()
        try:
            tasks = await self._seed(env.store, decision={parked[0]: reply}, satisfied=out_of_run)
            await env.executor.execute(self._plan(satisfied=out_of_run), tasks)
            return await self._judge(env.store)
        finally:
            await env.close()

    async def _continue_with_answer(self, record: dict[str, Any], node_id: str, asked: dict[str, dict[str, Any]],
                                    reply: str) -> tuple[bool, str, str]:
        """The step that asked runs again with the person's answer in hand.

        Like any continuation this is a fresh run over what has not finished, in the same workspace: what the step
        already wrote is still there, and the answer rides in the way the console's answer path delivers it
        (``user_supplied_input`` beside the question it answers). It is not a resumed model turn, and says so.
        """
        phases = {str(k): str(v) for k, v in dict(record.get("phases") or {}).items()}
        self.prior_node_receipts = {
            str(key): dict(value) for key, value in dict(record.get("node_receipts") or {}).items() if isinstance(value, dict)
        }
        self.answered = [dict(item) for item in list(record.get("answered") or []) if isinstance(item, dict)][-20:]
        self.answered.append({"node_id": node_id, "request": dict(asked[node_id]), "reply": reply})
        # The question is answered now. Written before anything runs: if this continuation dies before it judges, the
        # record must not still say the old question is waiting.
        try:
            (self.home / "trial.json").write_text(
                json.dumps({**record, "asked": {}, "answered": self.answered}, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError:
            pass
        finished = {nid for nid, phase in phases.items() if phase == Phase.APPROVED.value}
        self.skipped = {str(k): str(v) for k, v in dict(record.get("skipped") or {}).items()}
        self.continuation = int(record.get("continuation", 0) or 0) + 1
        self.run_id = f"{self.run_id}-c{self.continuation}"
        out_of_run = finished | set(self.skipped)
        self.run_nodes = [n for n in self.nodes if str(n.get("id")) not in out_of_run]
        self.item_ids = {str(n.get("id")): f"{self.spec_id}-{n.get('id')}-c{self.continuation}" for n in self.run_nodes}
        self.prior_phases = {nid: phases[nid] for nid in finished}
        env = await self._environment()
        try:
            tasks = await self._seed(env.store, decision={node_id: reply}, satisfied=out_of_run, answered=asked)
            await env.executor.execute(self._plan(satisfied=out_of_run), tasks)
            return await self._judge(env.store)
        finally:
            await env.close()

    def _choose_branches(self, decider: str, reply: str) -> tuple[bool, str, str] | None:
        """The reply that releases a human node picks among the branches hanging off it.

        A branch step's ``branch_when`` words have to appear in the reply.
        Branches not named are skipped, and so is anything that depends on
        skipped steps alone; a step that also depends on a live step keeps
        going with the skipped dependency dropped. No branch named at all is
        a refusal that lists the words to use.
        """
        children = [n for n in self.nodes if decider in [str(d) for d in list(n.get("depends_on") or [])]
                    and str(n.get("branch_when", "") or "").strip()]
        if not children:
            return None
        answer = "".join(str(reply or "").split())
        chosen = [n for n in children if "".join(str(n.get("branch_when")).split()) in answer]
        if not chosen:
            options = "；".join(f"{n.get('id')}：{n.get('branch_when')}" for n in children)
            return (False, decider, f"decision names no branch; say one of: {options}; attempt {self.home.name}")
        chosen_ids = {str(n.get("id")) for n in chosen}
        for n in children:
            if str(n.get("id")) not in chosen_ids:
                self.skipped[str(n.get("id"))] = f"branch not chosen at {decider}"
        # Anything downstream that only the skipped branches feed is skipped too.
        changed = True
        while changed:
            changed = False
            for n in self.nodes:
                nid = str(n.get("id"))
                deps = [str(d) for d in list(n.get("depends_on") or [])]
                if nid in self.skipped or not deps:
                    continue
                if all(d in self.skipped for d in deps):
                    self.skipped[nid] = "depends only on skipped branches"
                    changed = True
        return None

    # ---------------------------------------------------------------- setup
    async def _environment(self) -> _Environment:
        from ampuly.database.store import AmpulyStore
        from ampuly.layer2_organization.communication import CommunicationManager
        from ampuly.layer2_organization.company_mode import CompanyWorkItemExecutor
        from ampuly.layer2_organization.org_engine import OrgEngine
        from ampuly.layer3_agent.harness import HarnessScope
        from ampuly.layer3_agent.harness_native import NativeHarnessProvider
        from ampuly.layer3_agent.harness_sessions import HarnessSessionRegistry
        from ampuly.layer3_agent.runtime_v2.runtime import NativeRuntimeV2
        from ampuly.layer4_tools.file_ops import create_file_tools
        from ampuly.layer4_tools.registry import ToolRegistry
        from ampuly.layer5_memory.memory_manager import MemoryManager

        env = _Environment()
        store = AmpulyStore(self.home / "tasks.db")
        await store.initialize()
        env.store = store
        try:
            config = AmpulyConfig()
            config.org.company_profile = "custom"
            config.org.final_decider_role_id = self.final_role
            config.org.roles = self._roles()
            org = OrgEngine(config, self.home)
            communication = CommunicationManager(store, EventBus(), llm=None, org_engine=org)
            tools = ToolRegistry()
            for tool in create_file_tools():
                if tool.name in OFFLINE_TOOLS:
                    tools.register(tool)
            if callable(self.extra_tool_factory):
                generated = self.extra_tool_factory(store)
                if inspect.isawaitable(generated):
                    generated = await generated
                generated_by_name = {tool.name: tool for tool in list(generated or [])}
                undeclared = set(generated_by_name) - set(self.extra_tool_names)
                missing = set(self.extra_tool_names) - set(generated_by_name)
                if undeclared or missing:
                    raise RuntimeError(
                        "trial tool factory identity differs from its declared names: "
                        f"undeclared={sorted(undeclared)}, missing={sorted(missing)}"
                    )
                for tool in generated_by_name.values():
                    tools.register(tool)
            memory = MemoryManager(self.home, project_id=self.spec_id, store=store)
            from ampuly.plugins.office_ui.trial_permissions import TrialPermissionPolicy

            trial_policy = TrialPermissionPolicy({name: self.tool_argument_checks.get(name) for name in self.extra_tool_names})
            # One runtime per model a step names (nodes[].model), all on the same tools, memory and policy: a step
            # runs on its own model with that provider's key (LLMProvider.for_model), the others on the default.
            runtimes: dict[str, Any] = {}

            def runtime_for(model: str) -> Any:
                if model not in runtimes:
                    runtimes[model] = NativeRuntimeV2(
                        llm=self.llm.for_model(model) if model else self.llm, tool_registry=tools, config=config,
                        max_iterations=self.max_iterations, memory_manager=memory,
                        # Nobody is at the console to ask during a trial, so nothing asks: what reaches outside runs only if
                        # the caller admitted it by name (its declared extra tools), everything else that reaches is denied.
                        permission_policy=trial_policy,
                    )
                return runtimes[model]

            node_by_id = {str(n.get("id")): n for n in self.nodes}
            attempt = self

            class _Agent:
                async def execute(self, task: Task, on_progress: Any = None) -> Any:
                    node = node_by_id.get(str(task.metadata.get("work_item_projection_id", "")), {})
                    allowed = attempt._granted_tools(node)
                    model = str(node.get("model", "") or "").strip()
                    runtime = runtime_for(model)
                    # What this step's calls are sent with: the route, not a verified remote identity.
                    default_model = str(getattr(getattr(attempt.llm, "config", None), "default_model", "") or "")
                    attempt.model_routes[str(node.get("id", ""))] = {
                        "model": model or default_model, "source": "node" if model else "global",
                        "tier": str(node.get("tier", "") or "").strip(),
                    }
                    return await runtime.run(
                        attempt._system_prompt(node, str(task.assigned_to or "")), attempt._brief(node, task),
                        task=task, allowed_tools=allowed, on_progress=on_progress,
                    )

            harness = NativeHarnessProvider(_Agent(), HarnessSessionRegistry(self.home / "harness.db"))
            env.harness = harness
            workspace = str(self.workspace)

            async def execute(task: Task) -> Any:
                # Bind the server-owned execution identity when the session is
                # opened.  Passing only project/customer/environment made an
                # order-bound trial look like an attempt to smuggle a different
                # order through the adopted Task; NativeHarness correctly
                # rejected it.  Trial and production now use the same
                # fail-closed task/order/budget boundary.
                sid = await harness.create_session(
                    HarnessScope(
                        self.runtime_project_id, workspace, run_id=self.run_id,
                        customer_id=self.runtime_customer_id,
                        environment_id=self.runtime_environment_id,
                    ),
                    context={
                        "task_id": task.id,
                        "order_id": str(task.metadata.get("order_id", "") or ""),
                        "budget_id": str(task.metadata.get("budget_id", "") or ""),
                    },
                )
                env.sessions.append(sid)
                harness.adopt_task(sid, task)
                result = await harness.run_turn(sid, f"试跑：{task.title}")
                asked = dict(getattr(result, "artifacts", {}) or {}).get("pause_request")
                if isinstance(asked, dict) and asked.get("requires_user_input"):
                    self.questions[str(task.metadata.get("work_item_projection_id", "") or "")] = dict(asked)
                    # In the product the engine publishes this when a turn ends on a question; the dispatcher parks
                    # the item only once the pending checkpoint exists, and otherwise idles for two minutes first.
                    await checkpoint({
                        "checkpoint_type": "task_user_input", "project_id": task.project_id,
                        "session_id": task.session_id, "task_id": task.id,
                        "payload": {
                            "task_id": task.id, "session_id": task.session_id,
                            "execution_mode": str(task.metadata.get("execution_mode", "") or "company_mode"),
                            "task_ids": [task.id], "prompt": str(getattr(result, "content", "") or ""),
                            "pause_request": dict(asked), "review_level": "human",
                            "review_target_role_id": "", "review_chain_role_ids": [],
                            "source_event_id": f"trial-question:{task.id}:{self.continuation}",
                            "review_feedback_version": 0, "revision": 0,
                        },
                    })
                return result

            async def checkpoint(payload: dict[str, Any]) -> None:
                # The same publication the product uses; the dispatcher waits
                # on this durable fact before it parks the item.
                self.checkpoints.append(payload)
                await store.publish_owner_interaction_checkpoint(
                    ExecutionCheckpoint(**payload),
                    interaction_key=payload["payload"]["source_event_id"],
                )

            env.executor = CompanyWorkItemExecutor(
                org_engine=org, communication=communication, approval_engine=None, memory=None,
                execute_task=execute, save_task=store.save_task, store=store,
                checkpoint_callback=checkpoint, work_item_timeout=self.step_timeout_sec,
            )
            env.executor.node_invoke_callback = self.node_invoke_callback
            # A trial has no judge and is not a delivery: its own deliverable check is what it answers with. With a
            # gate that escalates when nobody judges (model-routing phase 0), every trial item would park until the
            # step timeout; the record says no gate ran.
            env.executor.gate_harness_disabled = True
            return env
        except BaseException:
            await env.close()
            raise

    def _granted_tools(self, node: dict[str, Any]) -> list[str]:
        """The tools a step may call in this attempt.

        A node trial's step names exactly what the step holds in production, so that is all it gets: a step whose
        role may only read cannot write its deliverable here either, and a pass is never evidence for a step the
        real run would refuse. An offline description has no role behind it and gets the whole offline file set.
        """
        declared = [str(t) for t in list(node.get("tools") or []) if str(t).strip()]
        if self.tools_as_declared:
            return list(dict.fromkeys(declared))
        return list(dict.fromkeys((*OFFLINE_TOOLS, *declared)))

    def _roles(self) -> list[RoleConfig]:
        """One role per name the draft uses, carrying its own nodes.

        Roles other than the final decider skip manager review: the offline
        home has nobody to be the manager, and a review turn would only be
        another model call about work the deliverable check judges anyway.
        """
        roles: dict[str, RoleConfig] = {}
        for node in self.nodes:
            role = _role_of(node)
            human = _is_human(node)
            if role not in roles:
                policy = {} if role == self.final_role else {"skip_manager_review": True}
                roles[role] = RoleConfig(
                    id=role, name=role,
                    responsibility=str(node.get("responsibility") or node.get("name") or role),
                    tools=[] if human else self._granted_tools(node),
                    runtime_policy=policy,
                    metadata={"workflow_nodes": []},
                )
            roles[role].metadata["workflow_nodes"].append(dict(node))
        return list(roles.values())

    def _deps(self, node: dict[str, Any], satisfied: frozenset[str] | set[str]) -> list[str]:
        return [str(d) for d in list(node.get("depends_on") or []) if str(d) not in satisfied]

    def _plan(self, *, satisfied: frozenset[str] | set[str] = frozenset()) -> Any:
        from ampuly.layer2_organization.org_work_item_planner import (
            CompanyWorkItemRuntimePlan,
            WorkItemProjectionSpec,
        )

        projections = [
            WorkItemProjectionSpec(
                projection_id=str(node.get("id")), role_id=_role_of(node), turn_type="execute",
                title=str(node.get("name") or node.get("id")),
                dependency_projection_ids=self._deps(node, satisfied),
            )
            for node in self.run_nodes
        ]
        return CompanyWorkItemRuntimePlan(
            profile="custom", final_decider_role_id=self.final_role, top_level_role_ids=[self.final_role],
            root_projection_id=str(self.run_nodes[-1].get("id")), projections=projections,
        )

    async def _seed(self, store: Any, *, decision: dict[str, str] | None = None,
                    satisfied: frozenset[str] | set[str] = frozenset(),
                    answered: dict[str, dict[str, Any]] | None = None) -> list[Task]:
        from ampuly.layer2_organization.work_item_links import set_linked_work_item_id

        workspace = str(self.workspace)
        await store.save_delegation_run(DelegationRun(
            run_id=self.run_id, project_id=self.runtime_project_id, session_id=self.run_id,
            company_profile="custom", execution_model="multi_team_org",
            final_decider_role_id=self.final_role, top_level_role_ids=[self.final_role],
            metadata={"workspace_root": workspace, "offline_trial": True, "continuation": self.continuation},
        ))
        tasks: list[Task] = []
        for node in self.run_nodes:
            node_id = str(node.get("id"))
            role = _role_of(node)
            deps = self._deps(node, satisfied)
            item_id = self.item_ids[node_id]
            title = str(node.get("name") or node_id)
            reply = str((decision or {}).get(node_id, "") or "")
            task = Task(
                id=f"task-{item_id}", title=title, description=self._description(node),
                project_id=self.runtime_project_id, session_id=self.run_id, assigned_to=role,
                status=TaskStatus.PENDING,
                # The same injection the console's answer path performs: the
                # decision rides in as user_supplied_input and the human node
                # judges it by its first verb.
                context_snapshot=(
                    {"user_supplied_input": reply, **({"requested_user_input": dict(answered[node_id])} if node_id in (answered or {}) else {})}
                    if reply else {}
                ),
                metadata={
                    "execution_mode": "company_mode", "runtime_model": "multi_team_org",
                    "delegation_run_id": self.run_id, "work_item_projection_id": node_id,
                    "customer_id": self.runtime_customer_id, "environment_id": self.runtime_environment_id,
                    "work_item_turn_type": "execute", "scope_key": node_id,
                    "delegation_seat_id": f"seat::{role}", "work_item_role_id": role,
                    "workspace_root": workspace, "parent_session_id": self.run_id,
                    **self.budget_context,
                    **({"trial_decision": reply[:400]} if reply else {}),
                },
            )
            set_linked_work_item_id(task, item_id)
            await store.save_delegation_work_item(DelegationWorkItem(
                work_item_id=item_id, run_id=self.run_id, role_id=role, seat_id=f"seat::{role}",
                title=title, summary=str(node.get("responsibility") or title),
                projection_id=node_id, phase=Phase.WAITING_DEPENDENCIES if deps else Phase.READY,
                kind="execute",
                metadata={
                    "runtime_model": "multi_team_org", "scope_key": node_id,
                    "dependency_work_item_ids": [self.item_ids[d] for d in deps if d in self.item_ids],
                },
            ))
            await store.save_task(task)
            if not await store.link_work_item_runtime_task(item_id, task.id):
                raise RuntimeError(f"could not link work item {item_id} to its task")
            tasks.append(task)
        return tasks

    def _system_prompt(self, node: dict[str, Any], role: str) -> str:
        standard = "；".join(str(s) for s in list(node.get("standard") or [])) or "按描述做完"
        deliverables = ", ".join(str(d) for d in list(node.get("deliverables") or []))
        tools = [str(t) for t in list(node.get("tools") or [])]
        tool_instruction = (
            "可用业务工具：" + ", ".join(tools) + "。只按本节点声明调用。\n"
            if tools else ""
        )
        file_instruction = (
            "用 file_write 把每个文件写进工作区，然后用一句话说明写了什么。"
            if deliverables else "完成声明动作并保留结构化回执，不要虚构文件产物。"
        )
        return (
            f"你是流程「{self.workflow.get('name') or self.spec_id}」里的「{role}」。\n"
            "现在是隔离试跑：没有客户、没有真实账号；模型调用仍受本次预算约束。工作区是你唯一能写的地方。\n"
            f"这一步：{node.get('name') or node.get('id')}\n"
            f"职责：{node.get('responsibility') or node.get('name') or ''}\n"
            f"做成什么样算做完：{standard}\n"
            f"必须交出的文件（相对工作区的路径）：{deliverables}\n"
            f"{tool_instruction}{file_instruction}"
        )

    def _brief(self, node: dict[str, Any], task: Task) -> str:
        """What the step is told to do -- and, on a continuation, what it asked a person and what they answered.

        A human node judges the person's words itself; an AI step can only use an answer it is shown. The question
        rides along so the answer is not read out of context.
        """
        snapshot = dict(getattr(task, "context_snapshot", {}) or {})
        answer = str(snapshot.get("user_supplied_input", "") or "").strip()
        if _is_human(node) or not answer:
            return task.description
        node_id = str(node.get("id", "") or "")
        history = [item for item in self.answered if str(item.get("node_id", "")) == node_id]
        if not history:
            history = [{"request": dict(snapshot.get("requested_user_input", {}) or {}), "reply": answer}]
        lines = [task.description, "", "你此前停下来向负责人提过问，下面是每一次的问题和回答（从早到晚）："]
        for index, item in enumerate(history[-8:], start=1):
            asked = dict(item.get("request") or {})
            lines.append(f"第 {index} 次提问：")
            lines.extend(f"- {q}" for q in [str(q) for q in list(asked.get("questions") or []) if str(q).strip()][:6])
            if str(asked.get("reason", "") or "").strip():
                lines.append(f"（原因：{asked['reason']}）")
            lines.extend(["负责人的回答：", str(item.get("reply", "") or "")])
        lines.append("请按这些回答把这一步做完；工作区里你之前写的文件都还在。已经回答过的不要再问。")
        return "\n".join(lines)

    def _description(self, node: dict[str, Any]) -> str:
        parts = [f"{node.get('name') or node.get('id')}：{node.get('responsibility') or ''}".rstrip("："),
                 f"交付物：{', '.join(str(d) for d in list(node.get('deliverables') or []))}"]
        inputs = list(node.get("inputs") or [])
        if inputs:
            parts.append(f"输入：{', '.join(str(i) for i in inputs)}")
        return "。".join(parts)

    # ---------------------------------------------------------------- judge
    async def _judge(self, store: Any) -> tuple[bool, str, str]:
        """Read every work item back and say what the run established."""
        phases: dict[str, Phase | None] = {}
        items: dict[str, Any] = {}
        tasks: dict[str, Any] = {}
        for node in self.nodes:
            node_id = str(node.get("id"))
            if node_id in self.skipped:
                items[node_id] = None
                phases[node_id] = None
            elif node_id in self.item_ids:
                item = await store.get_delegation_work_item(self.item_ids[node_id])
                items[node_id] = item
                phases[node_id] = getattr(item, "phase", None) if item is not None else None
                tasks[node_id] = await store.get_task(f"task-{self.item_ids[node_id]}")
            else:
                items[node_id] = None
                try:
                    phases[node_id] = Phase(self.prior_phases.get(node_id, ""))
                except ValueError:
                    phases[node_id] = None
        record: dict[str, Any] = {
            "run_id": self.run_id, "attempt": self.home.name, "workspace": str(self.workspace),
            "workflow_digest": workflow_digest(self.workflow), "continuation": self.continuation,
            "workflow_contract": {
                "workflow_id": str(self.workflow.get("id", "") or ""),
                "workflow_version": str(self.workflow.get("version", "") or ""),
                "skill_versions": {
                    str(key): str(value)
                    for key, value in dict(self.workflow.get("skill_versions", {}) or {}).items()
                },
            },
            "phases": {k: ("skipped" if k in self.skipped else v.value if isinstance(v, Phase) else str(v)) for k, v in phases.items()},
            "skipped": dict(self.skipped),
            "checkpoints": len(self.checkpoints),
            "gate": "not_run",
        }
        record["node_receipts"] = {
            node_id: {
                "node_id": node_id,
                "node_type": str(next((n.get("type", "") for n in self.nodes if str(n.get("id")) == node_id), "")),
                "status": (
                    "skipped" if node_id in self.skipped
                    else phase.value if isinstance(phase, Phase) else str(phase)
                ),
                "declared_inputs": [
                    str(value) for value in list(next((n.get("inputs", []) for n in self.nodes if str(n.get("id")) == node_id), []) or [])
                ],
                "artifact_refs": [],
                "component_receipt": dict(getattr(tasks.get(node_id), "metadata", {}) or {}).get("invoked_workflow", {}),
                "component_receipts": list(
                    dict(getattr(tasks.get(node_id), "metadata", {}) or {}).get("component_receipts", []) or []
                ),
                "budget_id": str(self.budget_context.get("budget_id", "") or ""),
                "continuation": self.continuation,
                **({"model_route": dict(self.model_routes[node_id])} if node_id in self.model_routes else {}),
            }
            for node_id, phase in phases.items()
        }
        # A continuation intentionally does not recreate completed tasks.  Its
        # evidence must therefore come from the prior durable record; replacing
        # those receipts with empty projections makes a recovered child order or
        # connector call disappear even though the scheduler correctly skipped
        # the work.  Only nodes in this continuation may overwrite their receipt.
        for node_id, prior in self.prior_node_receipts.items():
            if node_id not in self.item_ids and node_id in record["node_receipts"]:
                record["node_receipts"][node_id] = dict(prior)
        tag = f"attempt {self.home.name}"
        try:
            for node in self.nodes:
                node_id = str(node.get("id"))
                phase = phases[node_id]
                if phase in (Phase.FAILED, Phase.CANCELLED):
                    if _is_human(node):
                        return (False, node_id, f"human node rejected: {self._why(items[node_id])}; {tag}")
                    return (False, node_id, f"step ended {phase.value}: {self._why(items[node_id])}; {tag}")
            found: dict[str, list[str]] = {}
            for node in self.nodes:
                node_id = str(node.get("id"))
                if _is_human(node) or phases[node_id] != Phase.APPROVED:
                    continue
                missing: list[str] = []
                for declared in list(node.get("deliverables") or []):
                    # By the order-folder rule, in the attempt's own workspace (C5-1): ``board/*.png`` is delivered
                    # when a picture is there -- it was always "not produced" -- and ``round1/`` only when a file
                    # is in it, not because the folder was prepared above.
                    if not match_deliverable(self.workspace, str(declared)).files:
                        missing.append(str(declared))
                    else:
                        found.setdefault(node_id, []).append(str(declared))
                        record["node_receipts"][node_id]["artifact_refs"].append(str(declared))
                if missing:
                    record["missing"] = {node_id: missing}
                    return (False, node_id, f"declared deliverable(s) not produced in the workspace: {', '.join(missing)}; {tag}")
            record["delivered"] = found
            parked = [str(n.get("id")) for n in self.nodes if _is_human(n) and phases[str(n.get("id"))] == Phase.AWAITING_HUMAN]
            # An AI step waiting on a person because it asked one: only with the question on record. A step that is
            # waiting for any other reason is a stopped step, as before -- nobody would know what to answer.
            asked = [
                str(n.get("id")) for n in self.nodes
                if not _is_human(n) and phases[str(n.get("id"))] == Phase.AWAITING_HUMAN and str(n.get("id")) in self.questions
            ]
            not_reached = [str(n.get("id")) for n in self.nodes if phases[str(n.get("id"))] in _NOT_STARTED]
            record["parked"] = parked
            record["asked"] = {node_id: self.questions[node_id] for node_id in asked}
            record["answered"] = list(self.answered)
            record["not_reached"] = not_reached
            # The person who resumes this attempt chooses the branch, so the
            # record carries the words that choose it.
            record["branch_options"] = {
                node_id: [
                    {"id": str(n.get("id")), "when": str(n.get("branch_when", "") or "")}
                    for n in self.nodes
                    if node_id in [str(d) for d in list(n.get("depends_on") or [])]
                    and str(n.get("branch_when", "") or "").strip()
                ]
                for node_id in parked
            }
            record["branch_options"] = {k: v for k, v in record["branch_options"].items() if v}
            for node in self.nodes:
                node_id = str(node.get("id"))
                phase = phases[node_id]
                if phase == Phase.APPROVED or node_id in parked or node_id in asked or node_id in not_reached or node_id in self.skipped:
                    continue
                return (False, node_id, f"step stopped in phase {getattr(phase, 'value', phase)}: {self._why(items[node_id])}; {tag}")
            if asked:
                # Not a pass and not a defect: the step cannot go on without a person. The words are fixed so whoever
                # reads the round can tell this apart from a failure (workflow_node_trial does).
                return (False, asked[0], f"{QUESTION_MARK} {asked[0]} asked a person and waits for the answer; {tag}")
            if not_reached and not parked:
                return (False, not_reached[0], f"step never started and nothing was waiting on a person; {tag}")
            if parked and not_reached:
                return (
                    False, parked[0],
                    f"human gate: a person is needed at {parked[0]}; "
                    f"{len(not_reached)} step(s) behind it were not tried offline: {', '.join(not_reached)}; {tag}",
                )
            ran = [str(n.get("id")) for n in self.nodes if str(n.get("id")) not in parked and str(n.get("id")) not in self.skipped]
            summary = f"ran {len(ran)} step(s) in offline home {self.home}"
            if self.skipped:
                summary += f"; skipped {', '.join(sorted(self.skipped))} (branch not chosen)"
            summary += f"; parked at human gate {parked[0]}, waiting for a person" if parked else "; every step ran"
            options = list(record.get("branch_options", {}).get(parked[0], [])) if parked else []
            if options:
                summary += "; branches: " + "; ".join(f"{o['id']}={o['when']}" for o in options)
            if self.continuation:
                summary += f"; continued after a person decided (continuation {self.continuation})"
            return (True, "", f"{summary}; {tag}")
        finally:
            try:
                (self.home / "trial.json").write_text(
                    json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
                )
            except OSError:
                pass

    @staticmethod
    def _why(item: Any) -> str:
        metadata = dict(getattr(item, "metadata", {}) or {}) if item is not None else {}
        bits = [str(metadata[k]) for k in ("failure_reason", "error", "reason", "verdict", "blocker", "blocked_reason") if metadata.get(k)]
        return ("; ".join(bits) or "no reason recorded")[:200]
