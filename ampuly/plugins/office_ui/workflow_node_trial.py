"""One saved canvas node through the real, budgeted offline runtime.

The execution truth stays in the existing ProcessSpec + Builder round ledger.
Small JSON descriptors under ``<ampuly_home>/node_trials`` connect those generic
records back to a workflow package and record the attempt workspace/artifacts.
No second scheduler and no new database truth domain are introduced.

A step that depends on others is tried on *pinned* upstream results: a person
names, for each upstream AI step, one succeeded trial of this same saved
version. That trial's workspace -- as it was left, so a chain carries what its
own upstream handed it -- is copied into the new isolated workspace, and every
consumed file is recorded with its digest. Nothing is invented as input, the
allowance is the same, and a pinned trial is never release evidence: its
inputs came from separate runs, not from one run of the workflow.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ampuly.plugins.office_ui.builder_trial import claim_saved_trial, run_claimed_trial
from ampuly.plugins.office_ui.business_store import BusinessStore, OrderTransitionError
from ampuly.plugins.office_ui.order_folder import named_by_any
from ampuly.plugins.office_ui.process_spec import ProcessSpec, Step
from ampuly.plugins.office_ui.trial_runner import HUMAN_NODE_TYPES, OFFLINE_TOOLS, QUESTION_MARK, CompanyTrialRunner, ResumeTrialRunner
from ampuly.plugins.office_ui.workflow_packages import WorkflowPackage, project_nodes_onto_org, usable_by_project
from ampuly.core.config import replace_with_retry

PREFIX = "node_trial_"
INTERNAL = ".ampuly-comms"
MAX_PIN_FILES = 400
MAX_PIN_BYTES = 64 * 1024 * 1024
MAX_LISTED_INPUTS = 40
# What one trial's record holds. Past these the record says it is incomplete, and an incomplete record is not pinned.
MAX_RECORDED_FILES = 2000
MAX_WALKED_ENTRIES = 20000


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _spec_id(*, request_id: str, project_id: str, workflow_id: str, version: str, node_id: str) -> str:
    token = _digest([request_id, project_id, workflow_id, version, node_id])[:24]
    return f"{PREFIX}{token}"


def _descriptor_root(ampuly_home: Path) -> Path:
    root = Path(ampuly_home) / "node_trials"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _descriptor_path(ampuly_home: Path, spec_id: str) -> Path:
    return _descriptor_root(ampuly_home) / f"{spec_id}.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    replace_with_retry(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _node(package: WorkflowPackage, node_id: str) -> dict[str, Any]:
    wanted = str(node_id or "").strip()
    for item in package.nodes:
        if str(item.get("id", "") or "") == wanted:
            return dict(item)
    raise OrderTransitionError(f"workflow node {wanted!r} not found")


def _version(package: WorkflowPackage) -> str:
    return str(package.manifest.get("version", "") or "").strip()


def _refuse_other_projects_flow(package: WorkflowPackage, project_id: str) -> None:
    """One project is one customer: a flow derived for a project is tried in that project only (usable_by_project).

    A trial spends the asking project's allowance and records into it, so trying another customer's flow is refused
    before anything is saved or opened. The sentence reaches the owner's console as it is.
    """
    if not usable_by_project(package, project_id):
        raise OrderTransitionError("这条业务流是另一个项目专用的，不能在本项目试跑")


def _local_trial_tools() -> dict[str, Any]:
    """Tools a node trial may be given beyond the isolated file tools, by name.

    Admission rule, pinned by test for every entry: the tool declares no permission effect other than reading local
    files and writing the workspace (so: no network, no spend, no external side effect), and it takes its roots from
    the task, which in a trial is the attempt's isolated workspace. Anything that reaches outside -- a model, a
    paid generator, a channel -- does not belong here; it needs an allowance and its own admission.
    """
    return {}


LOCAL_TOOL_EFFECTS = frozenset({"local_read", "workspace_write"})
# Asking a person reaches nothing outside the runtime: the call ends the turn and the attempt waits for the answer.
ASK_TOOL = "request_user_input"
ASK_TOOL_EFFECTS = frozenset({"runtime_internal"})
MAX_QUESTION_CHARS = 4000


# The one paid tool a node trial admits. It reaches the network, so it is admitted by name to the trial's permission
# policy with a check that keeps its files inside the attempt's workspace, and it spends from an image ceiling that
# is part of the trial's allowance (policy: 2 by default, 6 at most; at the ceiling the turn stops and
# reports -- no retry, no pass). The default of 2 lives on the page that shows it: the server has none, so a client
# that never showed the paid-image confirmation cannot spend. The image tool and its default model are the ones a
# real order uses: a cheaper one would make the trial say nothing about the real thing.
IMAGE_TOOL = "image_generate"
MAX_IMAGE_LIMIT = 6
# A round that may generate images runs longer than the 120 s of an offline one, and its step a little shorter than
# the round, so the step's own deadline is the one that fires. An image call is refused -- before anything is sent --
# when less time is left than it can take (see trial_permissions.image_call_check).
IMAGE_ROUND_TIMEOUT_SEC = 600
IMAGE_STEP_TIMEOUT_SEC = 570


def _image_call_worst_case_sec(arguments: dict[str, Any]) -> float:
    """Submit, the provider's own polling window, and one download per image -- the tool's timeouts, added up."""
    from ampuly.layer4_tools.image_gen import _MAX_IMAGES, _POLL_TIMEOUT_S

    try:
        count = max(1, min(int(arguments.get("n") or 1), _MAX_IMAGES))
    except (TypeError, ValueError):
        count = _MAX_IMAGES
    return 60.0 + float(_POLL_TIMEOUT_S) + 60.0 * count


def _image_tool() -> Any:
    from ampuly.layer4_tools.image_gen import create_image_tools

    return next(tool for tool in create_image_tools() if tool.name == IMAGE_TOOL)


def _ask_tool() -> Any:
    from ampuly.layer4_tools.user_input import create_user_input_tool

    return create_user_input_tool()


def trial_tool_plan(tools: list[str]) -> dict[str, list[str]]:
    """Which of a step's tools a node trial provides, and which it cannot -- said before any allowance is opened."""
    from ampuly.plugins.office_ui.trial_runner import OFFLINE_TOOLS
    from ampuly.layer4_tools.selfmedia.public_fetch import PUBLIC_FETCH_TOOL

    local = _local_trial_tools()
    names = list(dict.fromkeys(str(item).strip() for item in tools if str(item).strip()))
    return {
        "file": [name for name in names if name in OFFLINE_TOOLS],
        "local": [name for name in names if name in local],
        "ask": [name for name in names if name == ASK_TOOL],
        "paid": [name for name in names if name == IMAGE_TOOL],
        "network": [name for name in names if name == PUBLIC_FETCH_TOOL],
        "unavailable": [name for name in names if name not in OFFLINE_TOOLS and name not in local
                        and name not in {ASK_TOOL, IMAGE_TOOL, PUBLIC_FETCH_TOOL}],
    }


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _redirected(path: Path) -> bool:
    """A symlink or a Windows junction: something that is here by name only."""
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _workspace_files(workspace: Path) -> tuple[list[Path], bool]:
    """Every file a trial left, and whether the walk had to stop early.

    The runtime's own message folders are left out. A link or a junction is never entered and never listed: what
    lies behind it is not something this trial wrote, and on Windows a junction looks like a plain directory to
    ``rglob``. The walk is bounded before anything is sorted or hashed.
    """
    found: list[Path] = []
    entries = 0
    pending = [workspace]
    while pending:
        directory = pending.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for item in children:
            entries += 1
            if entries > MAX_WALKED_ENTRIES or len(found) >= MAX_RECORDED_FILES:
                return sorted(found), True
            if directory == workspace and item.name == INTERNAL:
                continue
            if _redirected(item):
                continue
            if item.is_dir():
                pending.append(item)
            elif item.is_file():
                found.append(item)
    return sorted(found), False


def _plain_file_under(root: Path, relative: str) -> Path | None:
    """``root/relative`` when every component on the way is a real directory and the leaf a real file inside root."""
    parts = [part for part in str(relative or "").replace("\\", "/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    current = root
    for part in parts:
        current = current / part
        if _redirected(current):
            return None
    try:
        current.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return current if current.is_file() else None


def pin_plan(package: WorkflowPackage, node_id: str) -> dict[str, Any]:
    """Which upstream steps a trial of this node has to be pinned to.

    ``required`` are the nearest upstream AI steps. A human step that hands over no file is passed through -- a
    person's release carries no bytes, what they released does -- and is named in ``released_by_assumption`` so the
    trial says it assumed the release. Anything else upstream cannot be supplied by a node trial, and ``blocked``
    says why. ``optional`` are farther AI ancestors a person may pin as well.
    """
    nodes = {str(item.get("id", "") or ""): dict(item) for item in package.nodes}
    node = _node(package, node_id)
    required: list[str] = []
    assumed: list[str] = []
    blocked = ""
    blocked_code = ""
    blocked_step = ""
    seen: set[str] = set()

    def refuse(code: str, step: str, sentence: str) -> None:
        nonlocal blocked, blocked_code, blocked_step
        if not blocked:
            blocked, blocked_code, blocked_step = sentence, code, step

    def walk(dependencies: list[Any]) -> None:
        for raw in dependencies:
            dependency = str(raw or "").strip()
            if not dependency or dependency in seen:
                continue
            seen.add(dependency)
            upstream = nodes.get(dependency)
            if upstream is None:
                refuse("unknown_upstream", dependency,
                       f"it depends on {dependency!r}, which is not a step of the saved workflow")
                continue
            kind = str(upstream.get("type", "") or "")
            if kind == "ai_do":
                required.append(dependency)
            elif kind in HUMAN_NODE_TYPES:
                if [item for item in list(upstream.get("deliverables") or []) if str(item).strip()]:
                    refuse("human_hands_over_files", dependency,
                           f"the upstream human step {dependency!r} hands over files no node trial can produce; "
                           "run the saved workflow instead")
                    continue
                assumed.append(dependency)
                walk(list(upstream.get("depends_on") or []))
            else:
                refuse("upstream_not_pinnable", dependency,
                       f"the upstream step {dependency!r} is a {kind or 'step of an unknown kind'}; "
                       "its result cannot be pinned, run the saved workflow instead")

    walk(list(node.get("depends_on") or []))
    ancestors: list[str] = []
    frontier = [str(item) for item in list(node.get("depends_on") or [])]
    visited: set[str] = set()
    while frontier:
        current = frontier.pop(0)
        if current in visited or current not in nodes:
            continue
        visited.add(current)
        if str(nodes[current].get("type", "") or "") == "ai_do":
            ancestors.append(current)
        frontier.extend(str(item) for item in list(nodes[current].get("depends_on") or []))
    return {
        "required": required,
        "optional": [item for item in ancestors if item not in required],
        "released_by_assumption": assumed,
        "blocked": blocked,
        "blocked_code": blocked_code,
        "blocked_step": blocked_step,
    }


def is_current(descriptor: dict[str, Any], package: WorkflowPackage) -> bool:
    """Whether a recorded trial is of the step as it is saved now."""
    if str(descriptor.get("workflow_id", "")) != package.id:
        return False
    if str(descriptor.get("workflow_version", "")) != _version(package):
        return False
    try:
        node = _node(package, str(descriptor.get("node_id", "") or ""))
    except OrderTransitionError:
        return False
    return str(descriptor.get("node_digest", "")) == _digest(node)


def resolve_pins(
    business: BusinessStore, package: WorkflowPackage, node_id: str, pins: Any, *,
    ampuly_home: Path, project_id: str,
) -> list[dict[str, Any]]:
    """Check every named upstream trial and say exactly which files it contributes. Nothing is copied here."""
    plan = pin_plan(package, node_id)
    if plan["blocked"]:
        raise OrderTransitionError(f"this step cannot be tried on pinned results: {plan['blocked']}")
    if pins in (None, ""):
        pins = []
    if not isinstance(pins, list) or any(not isinstance(item, dict) for item in pins):
        raise OrderTransitionError("pins must be a list of {node_id, spec_id}")
    chosen: dict[str, str] = {}
    for item in pins:
        upstream = str(item.get("node_id", "") or "").strip()
        spec_id = str(item.get("spec_id", "") or "").strip()
        if not upstream or not spec_id.startswith(PREFIX) or not spec_id[len(PREFIX):].isalnum():
            raise OrderTransitionError("every pin names an upstream step and one of its node trials")
        if upstream in chosen:
            raise OrderTransitionError(f"the upstream step {upstream!r} is pinned twice")
        if upstream not in plan["required"] and upstream not in plan["optional"]:
            raise OrderTransitionError(f"{upstream!r} is not an upstream AI step of this node")
        chosen[upstream] = spec_id
    missing = [item for item in plan["required"] if item not in chosen]
    if missing:
        raise OrderTransitionError(
            "this node needs upstream artifacts; pin a succeeded trial of: " + ", ".join(missing)
        )

    resolved: list[dict[str, Any]] = []
    for upstream, spec_id in chosen.items():
        descriptor = _read_json(_descriptor_path(ampuly_home, spec_id))
        if not descriptor or str(descriptor.get("project_id", "")) != str(project_id):
            raise OrderTransitionError(f"the pinned trial of {upstream!r} was not found in this project")
        if str(descriptor.get("node_id", "")) != upstream:
            raise OrderTransitionError(f"the pinned trial is not a trial of {upstream!r}")
        if not is_current(descriptor, package):
            raise OrderTransitionError(
                f"the pinned trial of {upstream!r} is of an earlier saved version of that step; trial it again"
            )
        if _payload(descriptor, business)["state"] != "succeeded":
            raise OrderTransitionError(f"the pinned trial of {upstream!r} did not succeed")
        if descriptor.get("artifacts_truncated"):
            raise OrderTransitionError(
                f"the pinned trial of {upstream!r} left more files than a node trial records; run the saved workflow instead"
            )
        home = str(descriptor.get("attempt_home", "") or "")
        if not home or not (Path(home) / "workspace").is_dir():
            raise OrderTransitionError(f"the workspace of the pinned trial of {upstream!r} is gone; trial it again")
        recorded = [dict(item) for item in list(descriptor.get("artifacts") or []) if isinstance(item, dict)]
        if not recorded:
            raise OrderTransitionError(f"the pinned trial of {upstream!r} recorded no file to hand over")
        if any(not str(item.get("path", "")) or len(str(item.get("sha256", "") or "")) != 64 for item in recorded):
            raise OrderTransitionError(
                f"the pinned trial of {upstream!r} was recorded without file digests; trial that step again"
            )
        resolved.append({"node_id": upstream, "spec_id": spec_id, "workspace": str(Path(home) / "workspace"),
                         "files": [{"path": str(item["path"]), "size": int(item.get("size", 0) or 0),
                                    "sha256": str(item["sha256"])} for item in recorded]})
    return resolved


def verify_pins(pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read every recorded file of every pin and keep each path once. Disk work only: safe off the event loop.

    What is carried is exactly what the pinned trial recorded -- all of it, and nothing that appeared in its
    workspace afterwards. A recorded file that is missing, changed, or reached through a link refuses the pin.
    """
    owner_of: dict[str, tuple[str, str]] = {}
    count = 0
    total = 0
    verified: list[dict[str, Any]] = []
    for pin in pins:
        upstream = str(pin["node_id"])
        root = Path(str(pin["workspace"]))
        files: list[dict[str, Any]] = []
        for item in list(pin["files"]):
            relative = str(item["path"])
            count += 1
            total += int(item.get("size", 0) or 0)
            if count > MAX_PIN_FILES or total > MAX_PIN_BYTES:
                raise OrderTransitionError(
                    f"the pinned results are larger than a node trial carries "
                    f"({MAX_PIN_FILES} files / {MAX_PIN_BYTES // (1024 * 1024)} MB); run the saved workflow instead"
                )
            source = _plain_file_under(root, relative)
            if source is None:
                raise OrderTransitionError(
                    f"{relative}, which the pinned trial of {upstream!r} recorded, is no longer there; trial that step again"
                )
            if source.stat().st_size != int(item.get("size", 0) or 0) or _file_digest(source) != item["sha256"]:
                raise OrderTransitionError(
                    f"{relative} changed after the pinned trial of {upstream!r} recorded it; trial that step again"
                )
            earlier = owner_of.get(relative)
            if earlier is not None:
                if earlier[1] != item["sha256"]:
                    raise OrderTransitionError(
                        f"the pinned trials of {earlier[0]!r} and {upstream!r} carry different versions of "
                        f"{relative}; pin trials that come from the same upstream results"
                    )
                continue
            owner_of[relative] = (upstream, str(item["sha256"]))
            files.append(dict(item))
        verified.append({**pin, "files": files})
    return verified


def seed_workspace(workspace: Path, pins: list[dict[str, Any]]) -> None:
    """Copy the pinned files into a fresh attempt workspace, and refuse a file that is no longer what was pinned."""
    import shutil

    root = workspace.resolve()
    for pin in pins:
        location = str(pin.get("workspace", "") or "")
        if not location or not Path(location).is_absolute():
            raise RuntimeError(f"the pinned trial of {pin.get('node_id')!r} can no longer be located")
        source_root = Path(location)
        for item in list(pin.get("files") or []):
            relative = str(item.get("path", "") or "")
            target = (workspace / relative).resolve()
            if target == root or root not in target.parents:
                raise RuntimeError(f"pinned file {relative!r} points outside the trial workspace")
            source = _plain_file_under(source_root, relative)
            if source is None or _file_digest(source) != str(item.get("sha256", "")):
                raise RuntimeError(f"pinned file {relative!r} is no longer what was pinned")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def _production_role_tools(package: WorkflowPackage, role_id: str) -> list[str] | None:
    """The tools this role holds when the package runs: the team's role, plus what every node on it grants.

    Composed exactly as installing the package composes the running org (project_nodes_onto_org), so a trial
    cannot hold a step to a different grant than the real run. None when the package has no readable team config.
    """
    path = package.org_config_path
    if path is None or not path.is_file():
        return None
    try:
        base = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(base, dict):
        return None
    composed = project_nodes_onto_org(base, package)
    for role in list(composed.get("roles", []) or []):
        if isinstance(role, dict) and str(role.get("id", role.get("role_id", "")) or "").strip() == role_id:
            return [str(tool) for tool in list(role.get("tools", []) or []) if str(tool).strip()]
    return None


def _trial_grant(package: WorkflowPackage, node: dict[str, Any]) -> list[str]:
    """What a single-step trial gives the step: its own tools, plus the file tools its role holds in production.

    Only file tools are carried over from the role -- they are the ones a trial can provide inside its isolated
    workspace; anything else the role holds (a channel, a paid tool) needs its own admission and allowance and is
    not given by the role's say-so. A role that configures no tools at all is given every registered tool in
    production, so here it keeps the whole offline file set.
    """
    declared =[str(item) for item in list(node.get("tools") or []) if str(item).strip()]
    held = _production_role_tools(package, str(node.get("role", "") or "").strip())
    if held is None or not held:
        files = list(OFFLINE_TOOLS)
    else:
        files = [tool for tool in held if tool in OFFLINE_TOOLS]
    return list(dict.fromkeys([*declared, *files]))


# What the owner is told when a step that works in this computer's desktop or in a browser is sent to a single-step
# trial: the trial provides neither channel.
TRIAL_NO_DESKTOP = "这一步要操作本机桌面，单节点试跑不提供；请保存流程后正式运行"
TRIAL_NO_BROWSER = "这一步要操作浏览器，单节点试跑不提供；请保存流程后正式运行"
TRIAL_EMPLOYEE_RUNTIME_REQUIRED = (
    "此节点绑定了 AI 员工，普通节点／整图试跑不能验证其固定版本、知识与工具权限。"
    "请通过正式员工执行链进行试跑验证。"
)


def build_node_spec(
    package: WorkflowPackage, node_id: str, *, spec_id: str, actor: str,
    pins: list[dict[str, Any]] | None = None,
) -> ProcessSpec:
    node = _node(package, node_id)
    # ProcessSpec's ordinary trial step has no EmployeeRuntimeHost binding.
    # Presence, including an invalid/empty value, must never fall back to a role.
    if "employee_binding" in node:
        raise OrderTransitionError(TRIAL_EMPLOYEE_RUNTIME_REQUIRED)
    if package.status == "retired":
        raise OrderTransitionError("a retired workflow cannot start a new node trial")
    if str(node.get("type", "") or "") != "ai_do":
        raise OrderTransitionError("only an AI work node has a Provider turn to trial")
    # LP-B P-h : a trial step is built without a desktop or browser channel and its grant keeps file tools
    # only, so such a step's tools refused half-way through a paid trial. Said here, before an allowance or a round.
    from ampuly.plugins.office_ui.browser_connector import connector_of
    from ampuly.plugins.office_ui.desktop_connector import desktop_connector_of

    if desktop_connector_of(node) is not None:
        raise OrderTransitionError(TRIAL_NO_DESKTOP)
    if connector_of(node) is not None:
        raise OrderTransitionError(TRIAL_NO_BROWSER)
    if list(node.get("depends_on") or []) and pins is None:
        raise OrderTransitionError(
            "this node needs upstream artifacts; pin a succeeded trial of each upstream step, "
            "or run the saved workflow instead"
        )
    outputs = [str(item) for item in list(node.get("deliverables") or []) if str(item).strip()]
    if not outputs:
        raise OrderTransitionError("the node declares no deliverable to verify")
    unavailable = trial_tool_plan([str(item) for item in list(node.get("tools") or [])])["unavailable"]
    if unavailable:
        # Said here, before an allowance is opened and a round is claimed -- not as a failed run afterwards.
        raise OrderTransitionError(
            "a node trial cannot provide: " + ", ".join(unavailable) + "; run the saved workflow for this step instead"
        )
    role = str(node.get("role", "") or "").strip()
    if not role:
        raise OrderTransitionError("the node needs a saved organization role before a real trial")
    standards = [str(item) for item in list(node.get("standard") or []) if str(item).strip()]
    if not standards:
        standards = ["declared deliverables exist in the isolated trial workspace"]
    inputs = [str(item) for item in list(node.get("inputs") or []) if str(item).strip()]
    carried = [str(item["path"]) for pin in list(pins or []) for item in list(pin.get("files") or [])]
    inputs.extend(item for item in carried[:MAX_LISTED_INPUTS] if item not in inputs)
    if len(carried) > MAX_LISTED_INPUTS:
        inputs.append(f"(and {len(carried) - MAX_LISTED_INPUTS} more upstream files already in the workspace)")
    work = Step(
        id="trial_step",
        name=str(node.get("name") or node_id),
        role=role,
        inputs=inputs,
        outputs=outputs,
        writes=[str(item) for item in list(node.get("writes") or []) if str(item).strip()],
        tools=_trial_grant(package, node),
        skills=[str(item) for item in list(node.get("skills") or []) if str(item).strip()],
        responsibility=str(node.get("responsibility") or node.get("name") or node_id),
        standard=standards,
        # The step's own model: the trial runs on what the saved step would run on.
        model=str(node.get("model", "") or "").strip(),
    )
    review = Step(
        id="owner_review", name="Owner 核对单节点试跑", kind="human_review",
        role="owner", outputs=["owner-review.txt"], standard=["Owner 决定是否把这份试跑作为候选证据"],
        human_gate=True, gate_to="owner", depends_on=["trial_step"],
    )
    return ProcessSpec(
        spec_id=spec_id,
        name=f"{package.name} / {node.get('name') or node_id} / 单节点试跑",
        version="0.1.0",
        trigger="Owner 在搭建台明确点击真实试跑",
        finishes_when="真实运行到 Owner 复核点并留下隔离产物",
        steps=[work, review],
        acceptance_criteria=["Provider 回合真实结束", "声明的产物在隔离工作区存在"],
        data_scope="只允许本次隔离工作区，不接触客户账号、渠道或生产数据",
        author=str(actor or "owner"),
    )


def _status(rounds: list[dict[str, Any]]) -> str:
    if not rounds:
        return "queued"
    outcome = str(rounds[0].get("outcome", "") or "")
    return {
        "running": "running",
        "passed": "succeeded",
        "failed": "failed",
        "validated": "validated",
        "unrun": "outcome_unknown",
    }.get(outcome, "outcome_unknown")


def _payload(descriptor: dict[str, Any], business: BusinessStore) -> dict[str, Any]:
    rounds = business.builder_rounds(str(descriptor.get("spec_id", "") or ""))
    latest = rounds[0] if rounds else {}
    state = "failed" if descriptor.get("admission_error") else _status(rounds)
    pinned = bool(descriptor.get("pins"))
    assumed = bool(descriptor.get("released_by_assumption"))
    # A step of one whole-flow trial (workflow_flow_trial) is handed what its upstream left in that same run of the
    # same saved version: chained, not pinned. Admission checked every upstream belonged to that run.
    flow = dict(descriptor.get("flow_trial") or {})
    chained = pinned and bool(str(flow.get("flow_id", "") or "")) and bool(flow.get("chained"))
    # The runner verifies a step by whether its files exist. A promised file that upstream carried in and this step
    # never touched exists without the step having produced anything: that is not a success.
    unproduced = [str(item) for item in list(descriptor.get("unproduced") or [])]
    if state == "succeeded" and unproduced:
        state = "failed"
    if descriptor.get("execution_error") and state in {"queued", "running"}:
        state = "outcome_unknown"
    # The step asked a person and the attempt is parked on the answer. The round behind it closed as "not passed",
    # which is true of the round and wrong as a verdict on the step: nothing failed, someone has to answer.
    if descriptor.get("question") and not descriptor.get("abandoned") and state != "running":
        state = "waiting_for_answer"
    if descriptor.get("abandoned"):
        state = "abandoned"
    if descriptor.get("ended") and state not in {"running", "abandoned"}:
        state = "failed"
    return {
        **{key: value for key, value in descriptor.items() if key not in _DERIVED},
        "state": state,
        "round": latest,
        "budget": business.run_budget(str(descriptor.get("budget_id", "") or "")) or {},
        # Inputs from separate trials, or a release nobody gave, are not one run of the workflow.
        "counts_as_evidence": bool(latest.get("outcome") == "passed") and state == "succeeded" and not assumed
        and (not pinned or (chained and bool(flow.get("upstream_evidence")))),
        "pinned_upstream": pinned and not chained,
        "chained_upstream": chained,
    }


# Computed on every read; never trusted from the file, never written back as if they were facts of the run.
_DERIVED = frozenset({"state", "round", "budget", "counts_as_evidence", "pinned_upstream", "chained_upstream", "receipt", "current"})


def public_trial(payload: dict[str, Any]) -> dict[str, Any]:
    """What the page gets: everything but where the attempt lies on disk."""
    return {key: value for key, value in payload.items() if key != "attempt_home"}


def _public_pins(pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the descriptor keeps of a pin: which trial, which files, which digests -- not where they lay on disk."""
    return [
        {"node_id": pin["node_id"], "spec_id": pin["spec_id"], "files": list(pin["files"])}
        for pin in pins
    ]


async def start_node_trial(
    business: BusinessStore, package: WorkflowPackage, node_id: str, *,
    ampuly_home: Path, project_id: str, actor: str, request_id: str,
    model_call_limit: int = 12,
    pins: Any = None,
    image_limit: int | None = None,
    flow: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ProcessSpec, dict[str, Any]]:
    """``flow`` is set only by a whole-flow trial: ``{"flow_id": ...}``. Every pin must then be a trial of that
    same flow run, so the step runs on what its upstream really left in this run."""
    _refuse_other_projects_flow(package, project_id)
    flow_id = str(dict(flow or {}).get("flow_id", "") or "").strip()
    request = str(request_id or "").strip()[:128]
    if not request:
        raise OrderTransitionError("a node trial needs a request_id")
    if isinstance(model_call_limit, bool) or not 1 <= int(model_call_limit) <= 20:
        raise OrderTransitionError("model_call_limit must be between 1 and 20")
    if image_limit is not None and (isinstance(image_limit, bool) or not isinstance(image_limit, int) or not 0 <= image_limit <= MAX_IMAGE_LIMIT):
        raise OrderTransitionError(f"image_limit must be a whole number between 0 and {MAX_IMAGE_LIMIT}")
    spec_id = _spec_id(
        request_id=request, project_id=project_id, workflow_id=package.id,
        version=_version(package), node_id=node_id,
    )
    path = _descriptor_path(ampuly_home, spec_id)
    existing = _read_json(path) if path.exists() else {}
    node = _node(package, node_id)
    node_digest = _digest(node)
    wanted_pins = sorted(
        (str(item.get("node_id", "") or ""), str(item.get("spec_id", "") or ""))
        for item in (pins if isinstance(pins, list) else []) if isinstance(item, dict)
    )
    if existing:
        expected = {
            "request_id": request, "project_id": str(project_id), "workflow_id": package.id,
            "workflow_version": _version(package), "node_id": str(node_id), "node_digest": node_digest,
        }
        recorded_pins = sorted(
            (str(item.get("node_id", "")), str(item.get("spec_id", "")))
            for item in list(existing.get("pins") or []) if isinstance(item, dict)
        )
        recorded_flow = str(dict(existing.get("flow_trial") or {}).get("flow_id", "") or "")
        if any(str(existing.get(key, "")) != str(value) for key, value in expected.items()) or recorded_pins != wanted_pins \
                or recorded_flow != flow_id:
            raise OrderTransitionError("request_id resolves to a different saved node trial")
        return _payload(existing, business), build_node_spec(
            package, node_id, spec_id=spec_id, actor=actor, pins=list(existing.get("pins") or []),
        ), {}

    uses_images = bool(trial_tool_plan([str(item) for item in list(node.get("tools") or [])])["paid"])
    if uses_images and (image_limit is None or image_limit < 1):
        # No default on the server: the number is part of what the person confirmed. A page that never showed the
        # paid-image confirmation (an older bundle, another client) sends none, and spends nothing. Refused here,
        # before anything is saved.
        raise OrderTransitionError(
            f"this step generates images; a trial of it needs an image allowance of 1 to {MAX_IMAGE_LIMIT}, named by the person who starts it"
        )
    waiting = [
        item for item in list_node_trials(business, ampuly_home, project_id=str(project_id), workflow_id=package.id, node_id=str(node_id))
        if item.get("state") == "waiting_for_answer"
    ]
    if waiting:
        # Its question and its "abandon" are shown on the step's latest trial only: a newer trial would leave this one
        # with an open allowance and no way to reach it.
        raise OrderTransitionError("a trial of this step is waiting for an answer; answer it or abandon it first")
    resolved: list[dict[str, Any]] | None = None
    if list(node.get("depends_on") or []) or pins:
        named = resolve_pins(business, package, node_id, pins, ampuly_home=ampuly_home, project_id=str(project_id))
        if flow_id:
            upstream_evidence = True
            for pin in named:
                source = _read_json(_descriptor_path(ampuly_home, str(pin["spec_id"])))
                if str(dict(source.get("flow_trial") or {}).get("flow_id", "") or "") != flow_id:
                    raise OrderTransitionError(
                        f"the upstream trial of {pin['node_id']!r} is not from this flow trial; a whole-flow trial "
                        "only hands a step what its upstream left in the same run"
                    )
                upstream_evidence = upstream_evidence and bool(_payload(source, business)["counts_as_evidence"])
        resolved = await asyncio.to_thread(verify_pins, named)
    spec = build_node_spec(package, node_id, spec_id=spec_id, actor=actor, pins=resolved)
    business.save_process_spec(spec.to_payload(), actor=actor)
    # The image ceiling is always written, 0 for a step that generates none: a kind left out of the limits is
    # unlimited, and a trial must never have an unlimited paid resource.
    images = int(image_limit or 0) if uses_images else 0
    budget = business.create_run_budget(
        project_id=str(project_id), limits={"model_call": int(model_call_limit), "image": images}, actor=actor,
    )
    descriptor = {
        "spec_id": spec_id,
        "request_id": request,
        "project_id": str(project_id),
        "workflow_id": package.id,
        "workflow_version": _version(package),
        "node_id": str(node_id),
        "node_digest": node_digest,
        "budget_id": str(budget["budget_id"]),
        "model_call_limit": int(model_call_limit),
        "image_limit": images,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "attempt_home": "",
        "artifacts": [],
        "pins": _public_pins(resolved or []),
        "released_by_assumption": list(pin_plan(package, node_id)["released_by_assumption"]) if resolved is not None else [],
        "deliverables": [str(item) for item in list(node.get("deliverables") or []) if str(item).strip()],
        # The model the saved step asks for ("" = the configured default); what its calls were sent with is added
        # from the attempt's own record when it has run (model_route).
        "model_requested": str(node.get("model", "") or "").strip(),
        # ...and the tier, so a trial record answers 「这一步是按哪个档位跑的」 as well as 「用了哪个模型」.
        "tier_requested": str(node.get("tier", "") or "").strip(),
    }
    if flow_id:
        descriptor["flow_trial"] = {
            "flow_id": flow_id,
            "chained": [str(pin["node_id"]) for pin in (resolved or [])],
            "upstream_evidence": bool(upstream_evidence) if resolved else True,
        }
    try:
        _write_json(path, descriptor)
        frozen, claim = await claim_saved_trial(business, spec, actor=actor)
    except Exception as exc:
        business.close_run_budget(
            str(budget["budget_id"]), actor=actor, reason="node trial admission failed",
        )
        descriptor["admission_error"] = str(exc)
        try:
            _write_json(path, descriptor)
        except OSError:
            pass
        raise
    return _payload(descriptor, business), frozen, claim


def list_node_trials(
    business: BusinessStore, ampuly_home: Path, *, project_id: str,
    workflow_id: str = "", node_id: str = "", package: WorkflowPackage | None = None,
) -> list[dict[str, Any]]:
    """Recorded trials, newest first. With the saved package, each says whether it is of the step as saved now."""
    out: list[dict[str, Any]] = []
    for path in _descriptor_root(ampuly_home).glob(f"{PREFIX}*.json"):
        descriptor = _read_json(path)
        if str(descriptor.get("project_id", "")) != str(project_id):
            continue
        if workflow_id and str(descriptor.get("workflow_id", "")) != str(workflow_id):
            continue
        if node_id and str(descriptor.get("node_id", "")) != str(node_id):
            continue
        payload = _payload(descriptor, business)
        if package is not None:
            payload["current"] = is_current(descriptor, package)
        out.append(payload)
    out.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return out[:50]


def read_trial_artifact(ampuly_home: Path, *, project_id: str, spec_id: str, relative_path: str) -> dict[str, Any]:
    """One file of one recorded trial, through the deliverable preview. Only files the trial recorded are served."""
    from ampuly.plugins.office_ui.artifacts_service import preview_file_under

    wanted = str(spec_id or "").strip()
    if not wanted.startswith(PREFIX) or not wanted[len(PREFIX):].isalnum():
        raise ValueError("not a node trial")
    descriptor = _read_json(_descriptor_path(ampuly_home, wanted))
    if not descriptor or str(descriptor.get("project_id", "")) != str(project_id):
        raise FileNotFoundError("node trial not found in this project")
    recorded = {
        str(item.get("path", "")): item
        for item in list(descriptor.get("artifacts") or []) if isinstance(item, dict)
    }
    entry = recorded.get(str(relative_path or "").strip().replace("\\", "/"))
    if entry is None:
        raise FileNotFoundError("the trial recorded no such file")
    home = str(descriptor.get("attempt_home", "") or "")
    if not home:
        raise FileNotFoundError("the trial has no workspace yet")
    payload = preview_file_under(Path(home) / "workspace", str(entry["path"]))
    # Where the attempt lies on disk is not the page's to know.
    payload.pop("absolute_path", None)
    payload["spec_id"] = wanted
    payload["node_id"] = str(descriptor.get("node_id", "") or "")
    payload["from_upstream"] = bool(entry.get("from_upstream"))
    payload["changed_here"] = bool(entry.get("changed_here"))
    payload["trial"] = True
    return payload


def pin_candidates(
    business: BusinessStore, ampuly_home: Path, package: WorkflowPackage, node_id: str, *, project_id: str,
) -> dict[str, Any]:
    """The plan for one node plus, per upstream step, the trials a person can pin right now (newest first)."""
    # The plan names the flow's steps: another customer's flow is not laid out here either.
    _refuse_other_projects_flow(package, project_id)
    plan = pin_plan(package, node_id)
    # The page asks for the plan when a step is opened: say in the same answer whether the step's tools can be tried.
    try:
        unavailable = trial_tool_plan([str(item) for item in list(_node(package, node_id).get("tools") or [])])["unavailable"]
    except OrderTransitionError:
        unavailable = []
    if unavailable and not plan["blocked"]:
        plan = {**plan, "blocked": "a node trial cannot provide: " + ", ".join(unavailable),
                "blocked_code": "tools_unavailable", "blocked_step": ", ".join(unavailable)}
    trials = list_node_trials(business, ampuly_home, project_id=project_id, workflow_id=package.id, package=package)
    names = {str(item.get("id", "") or ""): str(item.get("name", "") or "") for item in package.nodes}
    upstream: list[dict[str, Any]] = []
    for step in [*plan["required"], *plan["optional"]]:
        usable = [
            {"spec_id": item["spec_id"], "created_at": item.get("created_at", ""),
             "files": len(list(item.get("artifacts") or [])), "pinned_upstream": bool(item.get("pinned_upstream"))}
            for item in trials
            if item.get("node_id") == step and item.get("state") == "succeeded" and item.get("current")
            and not item.get("artifacts_truncated") and list(item.get("artifacts") or [])
            and all(item_file.get("sha256") for item_file in list(item.get("artifacts") or []))
        ]
        upstream.append({"node_id": step, "name": names.get(step, ""), "required": step in plan["required"],
                         "candidates": usable[:10]})
    return {**plan, "upstream": upstream}


def _record_workspace(workspace: Path, carried: dict[str, str], deliverables: list[str]) -> dict[str, Any]:
    """What the attempt left, with digests. Disk work only: safe off the event loop."""
    files, truncated = _workspace_files(workspace)
    artifacts: list[dict[str, Any]] = []
    total = 0
    for item in files:
        relative = item.relative_to(workspace).as_posix()
        size = item.stat().st_size
        entry: dict[str, Any] = {"path": relative, "size": size}
        total += size
        if total > MAX_PIN_BYTES:
            # Listed, not hashed: a record without every digest is not something a later trial can be pinned to.
            truncated = True
        else:
            entry["sha256"] = _file_digest(item)
        if relative in carried:
            # Brought in from upstream; say so, and say if this step rewrote it.
            entry["from_upstream"] = True
            entry["changed_here"] = "sha256" in entry and carried[relative] != entry["sha256"]
        artifacts.append(entry)
    present = {item["path"] for item in artifacts}
    produced = [item for item in artifacts if not item.get("from_upstream") or item.get("changed_here")]
    # Which files a promised path names is the order-folder rule, the one the runner judged the step by (C5-1):
    # ``*`` within one name, ``dir/`` the files directly inside, hidden files never.
    unproduced = [
        pattern for pattern in deliverables
        if named_by_any(pattern, present) and not named_by_any(pattern, (item["path"] for item in produced))
    ]
    return {
        "artifacts": artifacts,
        "artifacts_truncated": truncated,
        # Carried in and gone afterwards: a later trial pinned to this one does not get them, and the reader is told.
        "removed_here": sorted(path for path in carried if path not in present),
        "unproduced": unproduced,
    }


def _runner_tools(spec: ProcessSpec) -> dict[str, Any]:
    """The extra tools this step names and a node trial provides -- exactly those, no more."""
    from ampuly.layer4_tools.selfmedia.public_fetch import (
        PUBLIC_FETCH_TOOL, check_arguments as public_fetch_check, create_public_fetch_tool,
    )

    plan = trial_tool_plan(list(next((step.tools for step in spec.steps if step.id == "trial_step"), [])))
    local_tools = _local_trial_tools()
    names = [*plan["local"], *plan["ask"], *plan["paid"], *plan["network"]]
    if not names:
        return {}
    tools: dict[str, Any] = {
        "extra_tool_factory": lambda _store: [
            *(local_tools[name]() for name in plan["local"]), *(_ask_tool() for _ in plan["ask"]), *(_image_tool() for _ in plan["paid"]),
            *(create_public_fetch_tool() for _ in plan["network"]),
        ],
        "extra_tool_names": tuple(names),
    }
    checks: dict[str, Any] = {}
    if plan["paid"]:
        from ampuly.plugins.office_ui.trial_permissions import image_call_check

        # The runner is built right before its round starts, so the round's clock starts here.
        started = time.monotonic()
        declared = dict(getattr(_image_tool(), "parameters", {}) or {}).get("properties") or {}
        # The image tool resolves an absolute file_path as given; in a trial this check is what keeps it inside.
        checks[IMAGE_TOOL] = image_call_check(
            declared, lambda: IMAGE_STEP_TIMEOUT_SEC - (time.monotonic() - started), _image_call_worst_case_sec,
        )
        tools["step_timeout_sec"] = IMAGE_STEP_TIMEOUT_SEC
    if plan["network"]:
        # 白名单/协议/无凭据在工具内再校验一次；这里先按参数拒绝，别让请求发出去。
        checks[PUBLIC_FETCH_TOOL] = public_fetch_check
    if checks:
        tools["tool_argument_checks"] = checks
    return tools


def _question_in(home: Path | None) -> dict[str, Any]:
    """What the attempt recorded an AI step asking, bounded for the page; {} when nobody asked."""
    if home is None:
        return {}
    record = _read_json(home / "trial.json")
    asked = {str(k): v for k, v in dict(record.get("asked") or {}).items() if isinstance(v, dict)}
    if not asked:
        return {}
    node_id, request = next(iter(asked.items()))
    questions = []
    for item in list(request.get("input_questions") or [])[:6]:
        if not isinstance(item, dict):
            continue
        questions.append({
            "id": str(item.get("id", "") or "")[:80],
            "header": str(item.get("header", "") or "")[:200],
            "question": str(item.get("question", "") or "")[:MAX_QUESTION_CHARS],
            "options": [str(option.get("label", option) if isinstance(option, dict) else option)[:200]
                        for option in list(item.get("options") or [])[:6]],
            "allow_freeform": item.get("allow_freeform") is not False,
        })
    try:
        attempt = int(str(home.name).rsplit("-", 1)[-1])
    except ValueError:
        attempt = 0
    return {
        "step": node_id, "attempt": attempt,
        "reason": str(request.get("reason", "") or "")[:MAX_QUESTION_CHARS],
        "questions": questions or [{"id": "", "header": "", "question": text[:MAX_QUESTION_CHARS], "options": [], "allow_freeform": True}
                                   for text in [str(q) for q in list(request.get("questions") or [])][:6]],
        "context_note": str(request.get("context_note", "") or "")[:MAX_QUESTION_CHARS],
    }


async def _run_and_record(
    business: BusinessStore, spec: ProcessSpec, claim: dict[str, Any], *, runner: Any, stored: dict[str, Any],
    pins: list[dict[str, Any]], ampuly_home: Path, actor: str,
) -> dict[str, Any]:
    """One run of the attempt (first or continued), then what it left -- and whether it now waits for a person."""
    path = _descriptor_path(ampuly_home, str(stored["spec_id"]))
    receipt: Any = None
    updated = dict(stored)
    updated.pop("question", None)
    try:
        paid = int(stored.get("image_limit", 0) or 0) > 0
        receipt = await run_claimed_trial(
            business, spec, claim, actor=actor, runner_factory=lambda: runner, paid=paid,
            timeout_seconds=float(IMAGE_ROUND_TIMEOUT_SEC) if paid else 120.0,
        )
    except BaseException as exc:
        # Cancelled at shutdown, or a runner that could not be stopped: the allowance still closes and the record
        # still says where the attempt is, instead of a trial that looks like it is running forever.
        updated["execution_error"] = f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        home = runner.last_home
        detail = str(dict(dict(receipt or {}).get("trial") or {}).get("detail", "") or "")
        question = _question_in(home) if detail.startswith(QUESTION_MARK) and not updated.get("execution_error") else {}
        if question and _rounds_left(business, str(stored["spec_id"])) < 1:
            # The step asked again and no round is left to answer it in: say that, instead of a question nobody can
            # answer sitting on an open allowance.
            updated["ended"] = "the step asked a person more times than one trial allows; run a new trial"
            question = {}
        if question:
            # Waiting on a person: the allowance stays open for the continuation, and closes when the step is
            # answered through to the end or the trial is abandoned.
            # The attempt knows the step by its compiled id; the page knows it by the node it opened.
            updated["question"] = {**question, "step": str(stored.get("node_id", "") or question["step"])}
        else:
            try:
                business.close_run_budget(str(stored["budget_id"]), actor=actor, reason="node trial finished")
            except Exception:
                pass
        updated["attempt_home"] = str(home or "")
        if home is not None:
            route = dict(dict(dict(_read_json(home / "trial.json").get("node_receipts") or {}).get("trial_step") or {}).get("model_route") or {})
            if route:
                updated["model_route"] = route
        carried = {
            str(item.get("path", "")): str(item.get("sha256", ""))
            for pin in pins for item in list(pin.get("files") or [])
        }
        if home is not None:
            try:
                updated.update(_record_workspace(home / "workspace", carried, list(stored.get("deliverables") or [])))
            except OSError as exc:
                updated["execution_error"] = updated.get("execution_error") or f"workspace could not be recorded: {exc}"
        if question:
            # Nothing is judged while the step waits: a promised file that is not there yet is not a verdict.
            updated["unproduced"] = []
        for kept in ("answers", "abandoned"):
            # Written by another request while this run was going on: never overwritten by the copy this run started from.
            current = _read_json(path).get(kept)
            if current:
                updated[kept] = current
        try:
            _write_json(path, updated)
        except OSError:
            pass
    return {**_payload(updated, business), "receipt": receipt}


def _located_pins(stored: dict[str, Any], ampuly_home: Path) -> list[dict[str, Any]]:
    # The descriptor keeps digests, not disk locations; the locations are looked up again from the pinned trials.
    pins = []
    for pin in list(stored.get("pins") or []):
        source = _read_json(_descriptor_path(ampuly_home, str(pin.get("spec_id", "") or "")))
        home = str(source.get("attempt_home", "") or "")
        pins.append({**pin, "workspace": str(Path(home) / "workspace") if home else ""})
    return pins


async def execute_node_trial(
    business: BusinessStore, spec: ProcessSpec, claim: dict[str, Any], *,
    descriptor: dict[str, Any], ampuly_home: Path, llm: Any, actor: str,
) -> dict[str, Any]:
    path = _descriptor_path(ampuly_home, str(descriptor["spec_id"]))
    # The file is the record; what the caller holds is a payload with derived fields the file must never keep.
    stored = _read_json(path) or {key: value for key, value in descriptor.items() if key not in _DERIVED}
    pins = _located_pins(stored, ampuly_home)
    runner = CompanyTrialRunner(
        llm=llm, ampuly_home=ampuly_home,
        workspace_seed=(lambda workspace: seed_workspace(workspace, pins)) if pins else None,
        budget_context={
            "budget_id": str(stored["budget_id"]),
            "project_id": str(stored["project_id"]),
            "customer_id": "", "environment_id": "", "order_id": "",
        },
        tools_as_declared=True,
        **_runner_tools(spec),
    )
    return await _run_and_record(business, spec, claim, runner=runner, stored=stored, pins=pins, ampuly_home=ampuly_home, actor=actor)


MAX_TRIAL_ROUNDS = 5  # business_store.reserve_builder_round's cap: the first run and every answer are one round each
ORPHANED_AFTER_SEC = 900  # no round runs this long (120 s, or 600 s for one that generates images): its process is gone


def _rounds_left(business: BusinessStore, spec_id: str) -> int:
    return MAX_TRIAL_ROUNDS - len(business.builder_rounds(spec_id))


def _round_running(business: BusinessStore, spec_id: str) -> bool:
    """Whether a round of this trial is running right now. A "running" round older than any run can be was left behind
    by a console that stopped mid-run; it is treated as gone so the trial can still be abandoned."""
    rounds = business.builder_rounds(spec_id)
    if not rounds or str(rounds[0].get("outcome", "")) != "running":
        return False
    try:
        started = datetime.fromisoformat(str(rounds[0].get("at", "") or ""))
    except ValueError:
        return True
    if started.tzinfo is None:
        started = started.astimezone()
    return (datetime.now(timezone.utc) - started).total_seconds() < ORPHANED_AFTER_SEC


def _waiting(ampuly_home: Path, *, project_id: str, spec_id: str) -> dict[str, Any]:
    wanted = str(spec_id or "").strip()
    if not wanted.startswith(PREFIX) or not wanted[len(PREFIX):].isalnum():
        raise OrderTransitionError("not a node trial")
    stored = _read_json(_descriptor_path(ampuly_home, wanted))
    if not stored or str(stored.get("project_id", "")) != str(project_id):
        raise OrderTransitionError("node trial not found in this project")
    if not stored.get("question") or stored.get("abandoned"):
        raise OrderTransitionError("this node trial is not waiting for an answer")
    return stored


async def answer_node_trial(
    business: BusinessStore, package: WorkflowPackage, *, ampuly_home: Path, project_id: str, spec_id: str,
    reply: str, actor: str,
) -> tuple[dict[str, Any], ProcessSpec, dict[str, Any]]:
    """Accept a person's answer to the question a step asked: a new round on the same attempt, same allowance.

    Refused when the step has been saved differently since (the question was about another step), when the answer is
    empty, and -- by the round claim -- while a round of this trial is still running. A continuation spends like a
    start, so another project's flow is refused here too (a trial left from before that was refused at the start).
    """
    _refuse_other_projects_flow(package, project_id)
    stored = _waiting(ampuly_home, project_id=project_id, spec_id=spec_id)
    if _round_running(business, str(stored["spec_id"])):
        raise OrderTransitionError("this trial is already continuing with an answer")
    answer = str(reply or "").strip()
    if len(answer) < 1:
        raise OrderTransitionError("an answer is needed, in words")
    if len(answer) > MAX_QUESTION_CHARS:
        raise OrderTransitionError(f"the answer is longer than {MAX_QUESTION_CHARS} characters")
    if not is_current(stored, package):
        raise OrderTransitionError("the step was saved again since it asked; abandon this trial and run a new one")
    spec = build_node_spec(package, str(stored["node_id"]), spec_id=str(stored["spec_id"]), actor=actor,
                           pins=list(stored.get("pins") or []) if list(_node(package, str(stored["node_id"])).get("depends_on") or []) else None)
    frozen, claim = await claim_saved_trial(business, spec, actor=actor)
    answers = [*list(stored.get("answers") or []), {"attempt": int(dict(stored["question"]).get("attempt", 0) or 0),
                                                    "at": datetime.now(timezone.utc).isoformat(), "by": str(actor or "owner")}]
    try:
        _write_json(_descriptor_path(ampuly_home, str(stored["spec_id"])), {**stored, "answers": answers[-20:]})
    except OSError:
        # The round is claimed but nothing will run it: settle it, or the trial reads "running" for ever.
        business.finish_builder_round(int(claim["id"]), {"outcome": "unrun", "reason": "the answer could not be recorded",
                                                         "detail": "", "failed_step": "", "spec_digest": str(claim.get("spec_digest", ""))}, actor=actor)
        raise
    return _payload({**stored, "answers": answers}, business), frozen, {**claim, "_reply": answer}


async def continue_node_trial(
    business: BusinessStore, spec: ProcessSpec, claim: dict[str, Any], *,
    descriptor: dict[str, Any], ampuly_home: Path, llm: Any, actor: str,
) -> dict[str, Any]:
    """Run the continuation ``answer_node_trial`` claimed: the step that asked runs again, in the same workspace, with
    the answer in hand. Nothing is seeded again and the allowance is the one the trial started with."""
    claim = dict(claim)
    reply = str(claim.pop("_reply", "") or "")
    path = _descriptor_path(ampuly_home, str(descriptor["spec_id"]))
    stored = _read_json(path) or {key: value for key, value in descriptor.items() if key not in _DERIVED}
    attempt = int(dict(stored.get("question") or {}).get("attempt", 0) or 0)
    runner = ResumeTrialRunner(
        attempt=attempt, reply=reply, llm=llm, ampuly_home=ampuly_home,
        budget_context={
            "budget_id": str(stored["budget_id"]),
            "project_id": str(stored["project_id"]),
            "customer_id": "", "environment_id": "", "order_id": "",
        },
        tools_as_declared=True,
        **_runner_tools(spec),
    )
    return await _run_and_record(business, spec, claim, runner=runner, stored=stored,
                                 pins=_located_pins(stored, ampuly_home), ampuly_home=ampuly_home, actor=actor)


def abandon_node_trial(business: BusinessStore, *, ampuly_home: Path, project_id: str, spec_id: str, actor: str) -> dict[str, Any]:
    """Stop waiting: the allowance closes and the trial says it was abandoned, not that it failed."""
    stored = _waiting(ampuly_home, project_id=project_id, spec_id=spec_id)
    if _round_running(business, str(stored["spec_id"])):
        # Closing the allowance now would cut the step off in the middle of using the answer it was just given.
        raise OrderTransitionError("this trial is continuing with an answer right now; wait for it to stop")
    business.close_run_budget(str(stored["budget_id"]), actor=actor, reason="node trial abandoned while waiting for an answer")
    updated = {**stored, "abandoned": {"at": datetime.now(timezone.utc).isoformat(), "by": str(actor or "owner")}}
    _write_json(_descriptor_path(ampuly_home, str(stored["spec_id"])), updated)
    return _payload(updated, business)
