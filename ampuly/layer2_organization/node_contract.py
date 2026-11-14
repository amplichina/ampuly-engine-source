"""The node contract: what a workflow package node means at run time.

A workflow package (plugin side, ``office_ui/workflow_packages.py``) projects
its node table onto each role's metadata when it is installed. This module
is the only place the kernel reads that table, so the three kernel hooks
(dispatcher, pause checkpoint, path check) stay one import away from a
single, testable set of pure functions:

* :func:`resolve_workflow_node` - which node a work item implements;
* :func:`human_node_prompt` - the card a person sees for a node they own;
* :func:`node_write_roots` / :func:`path_allowed_by_node` - the directories
  a node may write, and whether one mutation path falls inside them.

No node table in the org means every function answers "no node", and the
runtime behaves exactly as upstream.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

HUMAN_NODE_TYPES = frozenset({"human_do", "human_review"})
# "model": a workflow step's own model (the engine runs that step's agent on it; ampuly/plugins/office_ui/step_models.py).
# "tier": a step's model tier -- 轻 / 标准 / 强 -- which the order's frozen plan turns into a model
#   (ampuly/core/model_tiers.py). A step that names both runs on the model.
# "connector": a step's browser universal connector (sites, goal, postconditions; ampuly/plugins/office_ui/browser_connector.py);
# "browser": "none" on the other steps of a connector step's role -- the browser tools refuse them (Q05).
# "component_params": this step's own parameters for its skills / tools (4.7-A16; ampuly/plugins/office_ui/node_components.py
#   runtime_params), which the step's prompt states one line per component (step_settings_lines).
# "desktop": a step's desktop connector (apps, goal, done-when conditions, output; 4.7-A06,
#   ampuly/plugins/office_ui/desktop_connector.py), which its desktop tools read.
WORKFLOW_NODE_KEYS = ("id", "name", "type", "gate", "gate_to", "assignee", "writes", "standard", "deliverables", "revision_path", "invoke", "branch_when", "depends_on", "model", "tier", "connector", "browser", "rework_to", "component_params", "desktop", "employee_binding")


def _role_id_for_task(task: Any) -> str:
    metadata = dict(getattr(task, "metadata", {}) or {})
    return str(getattr(task, "assigned_to", "") or metadata.get("work_item_role_id", "") or "").strip()


def role_nodes(org_config: Any, role_id: str) -> list[dict[str, Any]]:
    """The node table projected onto ``role_id``, or an empty list."""
    if not role_id:
        return []
    roles = list(getattr(org_config, "roles", []) or [])
    for role in roles:
        candidate = str(getattr(role, "id", getattr(role, "role_id", "")) or "").strip()
        if candidate != role_id:
            continue
        metadata = dict(getattr(role, "metadata", {}) or {})
        return [dict(n) for n in list(metadata.get("workflow_nodes", []) or []) if isinstance(n, dict)]
    return []


def all_nodes(org_config: Any) -> list[dict[str, Any]]:
    """Every node in the org's table, deduplicated by id, in role order.

    A branch and the step that decides it usually belong to different
    roles, so a branch question can only be answered against the whole
    table, not one role's slice of it.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for role in list(getattr(org_config, "roles", []) or []):
        metadata = dict(getattr(role, "metadata", {}) or {})
        for node in list(metadata.get("workflow_nodes", []) or []):
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id", "") or "").strip()
            if not node_id or node_id in seen:
                continue
            seen.add(node_id)
            out.append(dict(node))
    return out


def node_dependencies(node: dict[str, Any]) -> list[str]:
    """The node ids this node waits for, as written in the package."""
    return [str(dep).strip() for dep in list(node.get("depends_on", []) or []) if str(dep).strip()]


def branch_children(nodes: list[dict[str, Any]], decider_node_id: str) -> list[dict[str, Any]]:
    """The branch steps hanging off one decider node."""
    decider = str(decider_node_id or "").strip()
    if not decider:
        return []
    return [
        node for node in nodes
        if decider in node_dependencies(node) and str(node.get("branch_when", "") or "").strip()
    ]


@dataclass(frozen=True)
class BranchDecision:
    """What one reply at a human node did to the branches below it."""

    decider_node_id: str
    chosen: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]
    options: tuple[tuple[str, str], ...]

    @property
    def undecided(self) -> bool:
        """True when the reply named no branch at all."""
        return not self.chosen

    def option_text(self) -> str:
        return "；".join(f"{node_id}：{when}" for node_id, when in self.options)


def branch_decision(org_config: Any, decider_node_id: str, reply: str) -> BranchDecision | None:
    """Read a release reply as a choice among the branches under that node.

    ``None`` means there is nothing to decide -- no branch hangs off this
    node -- and the caller behaves exactly as it did before branches
    existed. A branch is chosen when its ``branch_when`` words appear in
    the reply; the branches not chosen are skipped, and so is anything
    whose every dependency is skipped. A step that also depends on a live
    step is not skipped: it keeps its live dependency and loses the other.
    A reply that names no branch is left undecided for the caller to ask
    again -- guessing a branch is the one thing this must never do.
    """
    decider = str(decider_node_id or "").strip()
    nodes = all_nodes(org_config)
    children = branch_children(nodes, decider)
    if not children:
        return None
    options = tuple(
        (str(node.get("id", "") or ""), str(node.get("branch_when", "") or ""))
        for node in children
    )
    answer = "".join(str(reply or "").split())
    chosen = tuple(
        node_id for node_id, when in options
        if when.strip() and "".join(when.split()) in answer
    )
    if not chosen:
        return BranchDecision(decider_node_id=decider, chosen=(), skipped=(), options=options)
    skipped: dict[str, str] = {
        node_id: f"分支未被选择（决定在「{decider}」）" for node_id, _ in options if node_id not in chosen
    }
    changed = True
    while changed:
        changed = False
        for node in nodes:
            node_id = str(node.get("id", "") or "").strip()
            deps = node_dependencies(node)
            if not node_id or node_id in skipped or not deps or node_id in chosen:
                continue
            if all(dep in skipped for dep in deps):
                skipped[node_id] = "只依赖被跳过的分支"
                changed = True
    return BranchDecision(
        decider_node_id=decider,
        chosen=chosen,
        skipped=tuple(sorted(skipped.items())),
        options=options,
    )


def is_execute_turn(task: Any) -> bool:
    """Whether this turn does the work itself rather than report on or review it.

    Report / review turns are the runtime's own follow-ups to a work item;
    their titles quote the node name ("Report #1: lead_review 审单") but
    they are never the node itself (2026-09-03: a human node's report turn
    parked itself as a second human gate).
    """
    metadata = dict(getattr(task, "metadata", {}) or {})
    turn_type = str(metadata.get("work_item_turn_type", "") or metadata.get("delegation_turn_kind", "") or "").strip().lower()
    if turn_type and turn_type != "execute":
        return False
    return not (metadata.get("report_execution_work_item") or metadata.get("review_execution_work_item"))


def resolve_workflow_node(
    task: Any, org_config: Any, *, strict_scope: bool = False,
    workflow_id: str | None = None,
) -> dict[str, Any] | None:
    """Match a work item to its node: scope_key, then title, then the role's only node."""
    if type(strict_scope) is not bool:
        raise WorkflowNodeScopeError()
    if strict_scope:
        return _resolve_workflow_node_strict(task, org_config, workflow_id=workflow_id)
    nodes = role_nodes(org_config, _role_id_for_task(task))
    if not nodes:
        return None
    metadata = dict(getattr(task, "metadata", {}) or {})
    if not is_execute_turn(task):
        return None
    scope_key = str(metadata.get("scope_key", "") or metadata.get("requested_scope_key", "") or "").strip().lower()
    title = str(getattr(task, "title", "") or "").strip()
    for node in nodes:
        if scope_key and scope_key == str(node.get("id", "") or "").strip().lower():
            return node
    for node in nodes:
        name = str(node.get("name", "") or "").strip()
        if name and (name == title or name in title):
            return node
    return nodes[0] if len(nodes) == 1 else None


def node_summary(node: dict[str, Any]) -> dict[str, Any]:
    """The subset of a node the runtime keeps on the task (durable, small)."""
    summary = {key: node.get(key) for key in WORKFLOW_NODE_KEYS if key in node}
    if "employee_binding" in summary:
        # Portable design data, never a pin or an authorization. A task's copy
        # must not mutate the package/role binding it was projected from.
        summary["employee_binding"] = copy.deepcopy(summary["employee_binding"])
    return summary


def employee_bindings_unchanged(
    base_nodes: list[dict[str, Any]], final_nodes: list[dict[str, Any]],
) -> bool:
    """Whether complete, validated node tables preserve all portable bindings.

    Compare the final merge/preview, never raw partial edits: an omitted edit
    field preserves its binding. A bound-node deletion is an unbind too. This
    comparison grants nothing and does not establish runtime support.
    """
    def indexed(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
        if not isinstance(nodes, list):
            return None
        table: dict[str, dict[str, Any]] = {}
        for node in nodes:
            if not isinstance(node, dict):
                return None
            node_id = node.get("id")
            if not isinstance(node_id, str) or not node_id or node_id in table:
                return None
            table[node_id] = node
        return table

    before, after = indexed(base_nodes), indexed(final_nodes)
    if before is None or after is None:
        return False
    # Reuse the existing pure, bounded standard-JSON identity rather than dict
    # equality (True == 1 and 1 == 1.0 are not unchanged binding content).
    from ampuly.plugins.office_ui.ai_employee_contract import AIEmployeeContractError, canonical_json

    for node_id in before.keys() | after.keys():
        old_node, changed_node = before.get(node_id, {}), after.get(node_id, {})
        old_has = "employee_binding" in old_node
        changed_has = "employee_binding" in changed_node
        if not old_has and not changed_has:
            continue
        if not old_has or not changed_has:
            return False
        old, changed = old_node["employee_binding"], changed_node["employee_binding"]
        if type(old) is not dict or type(changed) is not dict:
            return False  # Stored null is malformed, not an absent binding.
        try:
            if canonical_json(old) != canonical_json(changed):
                return False
        except AIEmployeeContractError:
            return False
    return True


def param_carries_secret(key: Any, value: Any) -> bool:
    """Whether one step parameter must stay out of the model's prompt: a credential-named key (even one holding an
    environment reference -- the name of a variable is no business of the model's), or a secret-shaped value
    anywhere inside it."""
    from ampuly.cli.distribution import SECRET_KEY_RE, _secret_shaped

    if SECRET_KEY_RE.search(str(key)):
        return True
    if isinstance(value, dict):
        return any(param_carries_secret(k, v) for k, v in value.items())
    if isinstance(value, list):
        return any(param_carries_secret("", v) for v in value)
    return _secret_shaped(str(key), value)


def _param_text(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    return json.dumps(value, ensure_ascii=False)


def step_settings_lines(node: dict[str, Any] | None) -> list[str]:
    """What the running step is told about its own skill / tool parameters, one line per component.

    ``本步对技能「document-summary」的设置：tone=简洁`` -- words for the model, not arguments pushed into a tool call. A
    parameter that could be a credential is left out here as well as at projection: this is the last door before
    the prompt.
    """
    lines: list[str] = []
    for item in list(dict(node or {}).get("component_params", []) or []):
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref", "") or "").strip()
        params = item.get("params")
        if not ref or not isinstance(params, dict):
            continue
        pairs = [
            f"{str(key).strip()}={_param_text(value)}"
            for key, value in params.items()
            if str(key).strip() and not param_carries_secret(key, value)
        ]
        if pairs:
            label = "工具" if str(item.get("kind", "") or "") == "tool" else "技能"
            lines.append(f"本步对{label}「{ref}」的设置：" + "，".join(pairs))
    return lines


def is_human_node(node: dict[str, Any] | None) -> bool:
    return bool(node) and str(node.get("type", "") or "") in HUMAN_NODE_TYPES


def human_node_prompt(node: dict[str, Any], branches: list[dict[str, Any]] | None = None) -> str:
    """What the assigned person reads: responsibility, standard, delivery paths, what to reply.

    ``branches`` are the branch steps hanging off this node. They are listed
    with their conditions because the reply is what chooses among them, and
    nobody can choose what the card does not mention.
    """
    name = str(node.get("name", node.get("id", "")) or "")
    lines = [f"节点「{name}」分配给你（{str(node.get('assignee', '') or '')}）。"]
    responsibility = str(node.get("responsibility", "") or "").strip()
    if responsibility:
        lines.append(f"职责：{responsibility}")
    standard = [str(x) for x in list(node.get("standard", []) or []) if str(x).strip()]
    if standard:
        lines.append("交付标准：" + "；".join(standard))
    deliverables = [str(x) for x in list(node.get("deliverables", []) or []) if str(x).strip()]
    if deliverables:
        lines.append("交付路径：" + "、".join(deliverables))
    if str(node.get("type", "") or "") == "human_review":
        lines.append("请审核上游产物：回复「放行」或写明打回原因。")
    else:
        lines.append("完成后在这里回复交付说明（写清交付了什么、放在哪里）。")
    options = [
        (str(b.get("id", "") or ""), str(b.get("branch_when", "") or "").strip())
        for b in list(branches or [])
        if str(b.get("branch_when", "") or "").strip()
    ]
    if options:
        lines.append(
            "这一步下面有分支，你的答复决定走哪条——把选中那条的条件原话写进答复里："
            + "；".join(f"{node_id}「{when}」" for node_id, when in options)
            + "。没被选中的分支不会跑，也不算失败。"
        )
    return chr(10).join(lines)


def _normalize_root(root: str) -> str:
    text = str(root or "").strip().replace(chr(92), "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def node_write_roots(task: Any) -> list[str]:
    """Directories the task's node may write, relative to its output root (where relative tool paths start);
    empty = unrestricted."""
    metadata = dict(getattr(task, "metadata", {}) or {})
    node = dict(metadata.get("workflow_node", {}) or {})
    roots = [_normalize_root(x) for x in list(node.get("writes", []) or [])]
    return [r for r in roots if r]


def role_write_roots_for(org_config: Any, task: Any) -> list[str]:
    """Every directory the task's role may write through any of its nodes (data phase 1, C4).

    For an execute turn that matched none of its role's several nodes: until
    then such a turn was unrestricted. Empty -- still unrestricted -- when the
    role has no nodes or any of them declares no ``writes`` (that node may
    already write anywhere in the output tree).
    """
    nodes = role_nodes(org_config, _role_id_for_task(task))
    roots: list[str] = []
    for node in nodes:
        declared = [_normalize_root(x) for x in list(node.get("writes", []) or [])]
        declared = [r for r in declared if r]
        if not declared:
            return []
        roots.extend(r for r in declared if r not in roots)
    return roots


def stamp_role_writes(task: Any, org_config: Any, node: dict[str, Any] | None) -> list[str]:
    """Stamp (or clear) ``workflow_role_writes`` on a turn about to run; return what was stamped.

    Only an execute turn that matched no node gets its role's union; a matched
    node, or a report / review turn (which stays output-wide, still inside the
    order fence), carries none -- a stale stamp from an earlier turn is removed.
    """
    metadata = getattr(task, "metadata", None)
    if not isinstance(metadata, dict):
        return []
    metadata.pop("workflow_role_writes", None)
    if node is not None or not is_execute_turn(task):
        return []
    roots = role_write_roots_for(org_config, task)
    if roots:
        metadata["workflow_role_writes"] = roots
    return roots


def task_write_roots(task: Any) -> list[str]:
    """The write roots the file hook enforces: the task's node's, or -- on no node -- its role's stamped union."""
    metadata = dict(getattr(task, "metadata", {}) or {})
    if metadata.get("workflow_node"):
        return node_write_roots(task)
    roots = [_normalize_root(x) for x in list(metadata.get("workflow_role_writes", []) or [])]
    return [r for r in roots if r]


def path_allowed_by_node(raw_path: str, roots: list[str], *, workspace_root: str = "", output_root: str = "") -> bool:
    """Whether one mutation path lies under one of the node's writable roots.

    The roots are relative to the output root, which is where a relative tool
    path starts. Absolute paths are therefore measured from ``output_root``
    (``workspace_root`` when there is none): measuring them from the workspace
    refused an order step's own absolute path ``<order>/output/brief/a.md`` and
    allowed ``<order>/brief/a.md`` (data phase 1). Traversal ("..") is refused
    outright.
    """
    if not roots:
        return True
    text = str(raw_path or "").strip().replace(chr(92), "/")
    if not text:
        return False
    ws = str(output_root or workspace_root or "").strip().replace(chr(92), "/").rstrip("/")
    if PurePosixPath(text).is_absolute() or (len(text) > 1 and text[1] == ":"):
        if not ws:
            return False
        lowered, ws_lowered = text.lower(), ws.lower()
        if not lowered.startswith(ws_lowered + "/"):
            return False
        text = text[len(ws) + 1:]
    while text.startswith("./"):
        text = text[2:]
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return False
    rel = "/".join(parts)
    # A root is a folder: a mutation lands inside it. The folder's own name
    # as a file path made image_generate write its batch images and records
    # beside the folder, outside it (data phase 1 review).
    for root in roots:
        if rel.startswith(root + "/"):
            return True
    return False

# Explicit future private opt-in; these bounds are not permissions or source
# provenance. Legacy helpers above remain unchanged and are not enabled here.
_STRICT_WORKFLOW_ID = re.compile(r"[a-z][a-z0-9_-]{1,63}\Z")
_STRICT_NODE_ROLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_STRICT_METADATA_KEYS = 256
_STRICT_ROLES = 256
_STRICT_TABLE_NODES = 1000
_STRICT_NODE_KEYS = 64
_STRICT_MISSING = object()


class WorkflowNodeScopeError(ValueError):
    """Fixed scope failure; no IDs, metadata, projection, SQL or diagnostics."""

    def __init__(self) -> None:
        self.code = "workflow_node_scope_invalid"
        super().__init__("工作流节点作用域无法核验。")


def _strict_mapping(value: Any, maximum: int) -> dict[str, Any]:
    if type(value) is not dict:
        raise WorkflowNodeScopeError()
    size = len(value)
    if size > maximum:
        raise WorkflowNodeScopeError()
    snapshot = {}
    keys = iter(value)
    for _ in range(size):
        if len(value) != size:
            raise WorkflowNodeScopeError()
        key = next(keys, _STRICT_MISSING)
        if type(key) is not str:
            raise WorkflowNodeScopeError()
        snapshot[key] = value[key]
        if len(value) != size:
            raise WorkflowNodeScopeError()
    if next(keys, _STRICT_MISSING) is not _STRICT_MISSING or len(value) != size:
        raise WorkflowNodeScopeError()
    return snapshot


def _strict_list(value: Any, maximum: int) -> tuple:
    if type(value) is not list:
        raise WorkflowNodeScopeError()
    size = len(value)
    if size > maximum:
        raise WorkflowNodeScopeError()
    snapshot = []
    for index in range(size):
        if len(value) != size:
            raise WorkflowNodeScopeError()
        snapshot.append(value[index])
        if len(value) != size:
            raise WorkflowNodeScopeError()
    if len(value) != size:
        raise WorkflowNodeScopeError()
    return tuple(snapshot)


def _strict_id(value: Any, pattern: re.Pattern) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise WorkflowNodeScopeError()
    return value


def _strict_workflow_node(
    task: Any, org_config: Any, *, workflow_id: str | None,
) -> dict[str, Any]:
    workflow_id = _strict_id(workflow_id, _STRICT_WORKFLOW_ID)
    # Task/Organization/Role DTO getters are trusted host inputs, not an
    # arbitrary-descriptor sandbox. No caller value is coerced to str/bool.
    raw_metadata = getattr(task, "metadata", None)
    metadata = _strict_mapping(raw_metadata, _STRICT_METADATA_KEYS)
    turn_keys = ("work_item_turn_type", "delegation_turn_kind")
    present_turns = [key for key in turn_keys if key in metadata]
    if not present_turns:
        raise WorkflowNodeScopeError()
    for key in present_turns:
        if type(metadata[key]) is not str or metadata[key] != "execute":
            raise WorkflowNodeScopeError()
    for key in ("report_execution_work_item", "review_execution_work_item"):
        if key in metadata and metadata[key] is not False:
            raise WorkflowNodeScopeError()

    assigned = getattr(task, "assigned_to", None)
    role_id = None
    if assigned is not None and not (type(assigned) is str and assigned == ""):
        role_id = _strict_id(assigned, _STRICT_NODE_ROLE_ID)
    declared = metadata.get("work_item_role_id")
    if declared is not None and not (type(declared) is str and declared == ""):
        declared_role = _strict_id(declared, _STRICT_NODE_ROLE_ID)
        if role_id is not None and role_id != declared_role:
            raise WorkflowNodeScopeError()
        role_id = declared_role
    if role_id is None:
        raise WorkflowNodeScopeError()
    if len(raw_metadata) != len(metadata):
        raise WorkflowNodeScopeError()

    scope_keys = ("scope_key", "requested_scope_key")
    present_scopes = [key for key in scope_keys if key in metadata]
    if not present_scopes:
        raise WorkflowNodeScopeError()
    scope = _strict_id(metadata[present_scopes[0]], _STRICT_NODE_ROLE_ID)
    for key in present_scopes[1:]:
        if _strict_id(metadata[key], _STRICT_NODE_ROLE_ID) != scope:
            raise WorkflowNodeScopeError()

    raw_roles = getattr(org_config, "roles", None)
    roles = _strict_list(raw_roles, _STRICT_ROLES)
    matches = []
    for role in roles:
        candidate = getattr(role, "id", _STRICT_MISSING)
        if candidate is _STRICT_MISSING:
            candidate = getattr(role, "role_id", _STRICT_MISSING)
        candidate = _strict_id(candidate, _STRICT_NODE_ROLE_ID)
        if candidate == role_id:
            matches.append(role)
        if len(raw_roles) != len(roles):
            raise WorkflowNodeScopeError()
    if len(matches) != 1:
        raise WorkflowNodeScopeError()

    raw_role_metadata = getattr(matches[0], "metadata", None)
    role_metadata = _strict_mapping(raw_role_metadata, _STRICT_METADATA_KEYS)
    raw_tables = role_metadata.get("workflow_node_tables")
    tables = _strict_mapping(raw_tables, _STRICT_METADATA_KEYS)
    if workflow_id not in tables:
        raise WorkflowNodeScopeError()
    raw_nodes = tables[workflow_id]
    nodes = _strict_list(raw_nodes, _STRICT_TABLE_NODES)
    seen = set()
    selected = None
    for entry in nodes:
        node = _strict_mapping(entry, _STRICT_NODE_KEYS)
        node_id = _strict_id(node.get("id"), _STRICT_NODE_ROLE_ID)
        folded = node_id.lower()
        if folded in seen:
            raise WorkflowNodeScopeError()
        seen.add(folded)
        if node_id == scope:
            selected = node
        if len(raw_nodes) != len(nodes):
            raise WorkflowNodeScopeError()
    if (len(raw_metadata) != len(metadata) or len(raw_roles) != len(roles)
            or len(raw_role_metadata) != len(role_metadata)
            or len(raw_tables) != len(tables) or len(raw_nodes) != len(nodes)):
        raise WorkflowNodeScopeError()
    if selected is None:
        raise WorkflowNodeScopeError()
    # Bounded snapshots are not locks or frozen sources. Current projection
    # only, shallow legacy shape; nested data stays shared.
    # This is neither a frozen source nor a write/execute/admission grant.
    return dict(selected)


def _resolve_workflow_node_strict(
    task: Any, org_config: Any, *, workflow_id: str | None,
) -> dict[str, Any]:
    result = None
    failed = False
    try:
        result = _strict_workflow_node(task, org_config, workflow_id=workflow_id)
    except Exception:
        failed = True
    if failed:
        # Own handler has ended; external active exceptions/private traceback
        # locals are not globally sanitized. Cancellation is never caught.
        raise WorkflowNodeScopeError() from None
    return result
