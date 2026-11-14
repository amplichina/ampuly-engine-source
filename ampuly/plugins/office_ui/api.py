"""The console's internal API: typed methods and the role each action needs.

Architecture review 3.1: the console used to be a 94-way switch in the
WebSocket handler, with authorization bolted onto its entrance. This module
is the boundary the handler, the CLI bridge and tests share:

* :data:`ACTION_ROLES` declares, for every console action, the least role
  that may call it - ``anonymous`` (the login handshake), ``employee``
  (a person's own work) or ``owner`` (everything else). Unknown actions
  default to ``owner``, so a new handler is private until it says otherwise;
* :class:`ConsoleAPI` holds the business-layer operations (work orders and
  their launch) as plain methods over the business store, the workflow
  package registry and two injected callables for the parts that need the
  live engine (creating a session, sending the intake message).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from loguru import logger

from ampuly.layer2_organization.employee_binding_guard import (
    assert_employee_binding_runtime_available,
    employee_binding_runtime_findings,
)
from ampuly.plugins.office_ui.business_store import (
    AcceptanceRecord,
    BusinessOutboxRecord,
    BusinessStore,
    HANDLING_KEY,
    OrderTransitionError,
    WorkOrder,
)
from ampuly.plugins.office_ui.improvement_proposal import (
    DEFAULT_BENEFIT_SAMPLE,
    DEFAULT_WORSE_THRESHOLD,
    PROCESS_TARGET_KIND,
    ImprovementProposalError,
    bind_improvement_evaluation,
    create_improvement_proposal,
    decide_improvement_proposal,
    draft_connector_payload,
    draft_process_payload,
    draft_skill_payload,
    improvement_asset_identity,
    improvement_proposal_by_id,
    improvement_evaluation_spec,
    improvement_request_payload,
    judge_order_benefit,
    list_improvement_proposals,
    materialize_connector_candidate,
    materialize_process_candidate,
    materialize_skill_candidate,
    order_benefit_fact,
    order_run_evidence,
    project_improvement_root,
    record_improvement_benefit,
    record_improvement_evaluation,
    reject_improvement_proposal,
    select_benefit_samples,
)
from ampuly.layer3_agent.harness_evaluation import harness_evaluation_result
from ampuly.plugins.office_ui.rsi_evaluation import freeze_evaluation_spec
from ampuly.plugins.office_ui.connector_candidate import (
    ConnectorCandidate,
    ConnectorCandidateError,
    project_candidate_root,
    registration_by_name,
)
from ampuly.plugins.office_ui.skill_replacement import (
    SkillReplacementCandidate,
    SkillReplacementError,
    project_skill_candidate_root,
    list_skill_replacement_candidates,
    record_skill_replacement_evaluation,
    record_skill_replacement_test,
    review_skill_replacement_candidate,
    skill_replacement_by_id,
    skill_replacement_evaluation_spec,
)
from ampuly.plugins.office_ui.skill_activation import (
    SkillActivation,
    SkillActivationError,
    activate_skill,
    list_skill_activations,
    project_skill_activation_root,
    rollback_skill,
    skill_activation_by_id,
    stage_skill_activation,
)
from ampuly.plugins.office_ui.workflow_packages import (
    WorkflowPackage,
    package_candidate_fingerprint,
    preflight_invocation,
    prepare_invocation,
    validate_invocation,
    publish_package,
    observed_package_revision,
    record_test_run,
    simulate_test_run,
    provider_trial_preflight,
    request_release_approval,
    decide_release_approval,
    rollback_package,
    start_rollout,
    record_rollout_observation,
    evaluate_rollout,
    finalize_rollout,
    auto_rollback_package,
    create_workflow_draft,
    derived_draft_id,
    update_package_nodes,
    submit_for_testing,
    usable_by_project,
)

ANONYMOUS = "anonymous"
EMPLOYEE = "employee"
OWNER = "owner"
# S3-6A 客户页: the level of the two actions a customer account calls as itself (the owner may call them too; an
# employee may not -- a customer's own page is not company work).
CUSTOMER = "customer"

ACTION_ROLES: dict[str, str] = {
    "employee_instance_create": OWNER,
    "employee_instance_adopt": OWNER,
    "employee_package_test_prepare": OWNER,
    "employee_package_test_start": OWNER,
    "employee_package_test_detail": OWNER,
    "employee_package_test_lookup": OWNER,
    "employee_package_review": OWNER,
    "employee_package_publish": OWNER,
    "employee_package_publish_lookup": OWNER,
    "employee_package_lifecycle_history": OWNER,
    "employee_package_test_abandon": OWNER,
    "employee_workflow_trial_prepare": OWNER,
    "employee_workflow_trial_start": OWNER,
    "employee_workflow_trial_read": OWNER,
    "employee_binding_candidates": OWNER,
    "employee_binding_resolve_set": OWNER,
    "employee_handoff_list": OWNER,
    "employee_handoff_decide": OWNER,
    "employee_handoff_rework": OWNER,
    "employee_handoff_continue": OWNER,
    "employee_handoff_check": OWNER,
    "employee_handoff_node_list": OWNER,
    # AE00 section 23: observation only, never confirmation/redispatch.
    "human_review_rework_inspect": OWNER,
    "human_review_rework_pending": OWNER,
    "human_review_rework_lookup": OWNER,
    "human_review_rework_confirm": OWNER,
    # login handshake
    "ping": ANONYMOUS,
    # a person's own gates: the panel checks the assignee itself
    "gate_detail": EMPLOYEE,
    "work_item_retry": OWNER,
    "gate_decide": EMPLOYEE,
    "auth_login": ANONYMOUS,
    "auth_token": ANONYMOUS,
    # a person's own work
    "auth_logout": EMPLOYEE,
    "account_set_password": EMPLOYEE,
    "my_tasks_list": EMPLOYEE,
    "interaction_reply": EMPLOYEE,
    "order_list": EMPLOYEE,
    "order_get": EMPLOYEE,
    "observation_audit_list": EMPLOYEE,
    "order_review": EMPLOYEE,  # the store checks the reviewer; owners always pass
    "acceptance_get": EMPLOYEE,
    "acceptance_define": OWNER,
    "acceptance_evidence": OWNER,
    "acceptance_submit": OWNER,
    "acceptance_decide": OWNER,
    "acceptance_rework": OWNER,
    "acceptance_round_export": OWNER,
    "portfolio_export": OWNER,
    "fde_control_plane_update": OWNER,
    "order_handling_update": OWNER,
    # 4.7-A08 分发授权单: listing, drafting, approving and revoking are all the owner's.
    "distribution_authorization_list": OWNER,
    "distribution_authorization_draft": OWNER,
    "distribution_authorization_approve": OWNER,
    "distribution_authorization_revoke": OWNER,
    "workflow_update_nodes": OWNER,
    "workflow_preflight": OWNER,
    "workflow_simulate_test": OWNER,
    "workflow_provider_preflight": OWNER,
    "workflow_node_trial_start": OWNER,
    "workflow_node_trial_list": OWNER,
    "workflow_flow_trial_start": OWNER,
    "workflow_flow_trial_continue": OWNER,
    "workflow_flow_trial_stop": OWNER,
    "workflow_flow_trial_list": OWNER,
    "workflow_rollback": OWNER,
    "workflow_rollout_start": OWNER,
    "workflow_rollout_observe": OWNER,
    "workflow_rollout_evaluate": OWNER,
    "workflow_rollout_finalize": OWNER,
    "workflow_rollout_rollback": OWNER,
    "workflow_redistribution_authorize": OWNER,
    "workflow_distribution_export": OWNER,
    "workflow_distribution_receive": OWNER,
    "workflow_distribution_inbox": OWNER,
    "workflow_record_test": OWNER,
    "workflow_create_draft": OWNER,
    "workflow_create_blank": OWNER,
    "workflow_derive_for_project": OWNER,
    "workflow_create_from_flow_draft": OWNER,
    "workflow_component_candidate_target": OWNER,
    "workflow_adopt_component_draft": OWNER,
    "project_workflow_adoptions": OWNER,
    "project_workflow_adopt": OWNER,
    "project_workflow_adoption_status": OWNER,
    "workflow_versions": OWNER,
    "component_detail": OWNER,
    "component_versions": OWNER,
    "component_edit": OWNER,
    "component_candidate_test": OWNER,
    "component_candidate_review": OWNER,
    "component_candidate_revise": OWNER,
    "component_candidate_evaluation_spec": OWNER,
    "component_candidate_evaluation_sessions": OWNER,
    "component_candidate_evaluation_record": OWNER,
    "workflow_submit_testing": OWNER,
    "workflow_request_release": OWNER,
    "workflow_decide_release": OWNER,
    "improvement_proposal_list": OWNER,
    "improvement_proposal_create": OWNER,
    "improvement_proposal_create_from_acceptance": OWNER,
    "improvement_proposal_reject": OWNER,
    "improvement_proposal_materialize_connector": OWNER,
    "improvement_proposal_materialize_process": OWNER,
    "improvement_proposal_materialize_skill": OWNER,
    "improvement_proposal_bind_evaluation": OWNER,
    "improvement_proposal_record_evaluation": OWNER,
    "improvement_proposal_bind_harness_sessions": OWNER,
    "improvement_proposal_record_harness_evaluation": OWNER,
    # 4.7-A11: 提一个改进 on a verified asset's detail, and the approval judged on real orders.
    "improvement_proposal_start": OWNER,
    "improvement_proposal_approve": OWNER,
    "skill_improvement_record_test": OWNER,
    "skill_improvement_review": OWNER,
    "skill_improvement_list": OWNER,
    "skill_improvement_stage": OWNER,
    "skill_improvement_activate": OWNER,
    "skill_improvement_rollback": OWNER,
    # 资料: read-only views of one project's own files (data phase 2). Declared here rather than left to the
    # owner-by-default rule, so the table says who may read a customer's delivered files.
    "data_project_index": OWNER,
    "data_order_view": OWNER,
    "data_folder_list": OWNER,
    "data_preview": OWNER,
    "data_attachments": OWNER,
    "data_usage": OWNER,
    "data_locations": OWNER,
    # 清掉一个备份计划任务。只清产品自己装的那几个；policy 要的「查看和清空」的清空那半。
    "forget_backup_task": OWNER,
    # 数据与文件 P3: 备份列表。只读，但它说的是这台机器上的绝对路径，所以只给 owner。
    "data_backups": OWNER,
    # 数据与文件 P3: 「看看会删掉什么」。只列不删——真正删除是 owner 要先确认的时刻，本期没有那条通路。
    "data_cleanup_preview": OWNER,
    "data_download": OWNER,
    "data_reveal": OWNER,
    "data_dir_browse": OWNER,
    "data_dir_pick": OWNER,
    "data_dir_create": OWNER,
    "data_folder_plan": OWNER,
    "data_folder_set": OWNER,
    "data_folder_legacy": OWNER,
    # Model assignment (模型分配). The default for an unnamed action is already OWNER; these are named so the
    # table states the rule rather than leaving it to a fallback.
    "settings_set_model_tiers": OWNER,
    "order_model_plan": OWNER,
    "data_folder_import": OWNER,
    "archive_project": OWNER,
    "project_lifecycle": OWNER,
    # S3-2A 项目中心: a project's business boundary and its four connection plans are the owner's to read and write.
    "project_profile_get": OWNER,
    "project_profile_set": OWNER,
    "project_connections_get": OWNER,
    "project_connections_set": OWNER,
    "restore_project": OWNER,
    "export_project": OWNER,
    "backup_project": OWNER,
    "restore_backup": OWNER,
    "destroy_project": OWNER,
    # 数据与文件 P3: 删掉一个项目会带走什么、不带走什么，以及现在能不能删。只读。
    "plan_project_removal": OWNER,
    # S3-3A 总控: 「等你处理」 across every project, and where a card lives. The owner's reads, named so the table says so.
    "owner_attention_list": OWNER,
    "checkpoint_locate": OWNER,
    # T11 step 2: owner desktop takeover / hand-back (not a model tool).
    "desktop_session_control": OWNER,
    "browser_account_inspect": OWNER,
    # S3-6A 客户页: a customer account's own orders as the customer projection, and its acceptance decision on one of
    # them (never the close: 确认收货并结案 stays acceptance_decide, the owner's). CUSTOMER here means "the customer
    # role and the owner"; an employee is refused.
    "customer_orders_view": CUSTOMER,
    "customer_accept": CUSTOMER,
    # everything else is the owner's: org, workflows, sessions, settings ...
}

# S3-6A: the whole of what a customer account may call -- not a rung on the anonymous < employee < owner ladder. A
# customer is outside the company, so it gets none of an employee's reads: no my_tasks_list, gate_detail, order_get,
# acceptance_get, observation_audit_list, order_review (work items, evidence with its runs and paths, the audit
# trail are the company's). It gets the handshake, its own password (the handler holds a non-owner to its own login),
# ``order_list`` narrowed to its customers and answered in customer rows (ws_handler ``_customer_order_list``), its
# orders as the customer projection (``customer_orders_view``: the preview's fields, deliverable names included) and
# ``customer_accept``. Left out on purpose: order_metrics / run_budget_* (money), artifacts_list / artifact_read /
# data_* (files by path), every push (``broadcast`` already sends to owners only), and anything of another customer.
CUSTOMER_ALLOWED_ACTIONS = frozenset({
    "ping", "auth_login", "auth_token", "auth_logout",
    "account_set_password",
    "order_list",
    "customer_orders_view",
    "customer_accept",
})


def required_role(action: str) -> str:
    return ACTION_ROLES.get(str(action or ""), OWNER)


def role_allows(account_role: str | None, action: str, *, auth_required: bool) -> bool:
    """The whole authorization rule: open console, anonymous, employee, owner -- and the customer's own short list.

    Before S3-6A an unknown role was answered as an employee; a ``customer`` account is now held to
    ``CUSTOMER_ALLOWED_ACTIONS`` and nothing else, whatever the table says of the action.
    """
    if not auth_required:
        return True
    needed = required_role(action)
    if account_role is None:
        return needed == ANONYMOUS
    if account_role == OWNER:
        return True
    if account_role == CUSTOMER:
        return action in CUSTOMER_ALLOWED_ACTIONS
    return needed in {ANONYMOUS, EMPLOYEE}


@dataclass
class LaunchReceipt:
    task_id: str
    message: str
    org_id: str


class LaunchFn(Protocol):
    async def __call__(
        self,
        project_id: str,
        package: WorkflowPackage,
        fields: dict[str, Any],
        *,
        order_id: str = "",
        command_id: str = "",
        model_plan: dict[str, Any] | None = None,
        launch_source: Any | None = None,
        record_creation_origin: bool = False,
    ) -> LaunchReceipt: ...


class AcceptanceReworkFn(Protocol):
    async def __call__(
        self,
        *,
        project_id: str,
        launched_task_id: str,
        target_node: str,
        feedback: str,
        request_id: str,
        command_id: str,
    ) -> dict[str, str]: ...


_DUAL_RUN_SESSIONS_FILE = "evaluation-sessions.json"


def _registered_dual_run_sessions(candidate_root: Path) -> dict[str, list[str]]:
    """The two groups of Harness sessions registered for a skill candidate's dual run ({} when none yet)."""
    import json as _json

    path = Path(candidate_root) / _DUAL_RUN_SESSIONS_FILE
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    groups = dict(data.get("groups", {}) or {}) if isinstance(data, dict) else {}
    return {str(k): [str(v) for v in list(ids or [])] for k, ids in groups.items() if isinstance(ids, list)}


def _write_registered_dual_run_sessions(candidate_root: Path, spec_fingerprint: str, groups: dict[str, list[str]]) -> None:
    from ampuly.plugins.office_ui.skill_replacement import _atomic_json

    _atomic_json(Path(candidate_root) / _DUAL_RUN_SESSIONS_FILE,
                 {"schema_version": 1, "spec_fingerprint": spec_fingerprint, "groups": groups})


def batch_lists_of(package: Any) -> list[tuple[str, str]]:
    """``(list name, item key)`` of every batch step in a flow (批量自动化: a step that sends a list out row by row).

    Read from the steps' own connector settings, the same normalised summary the running step reads. Empty for a
    flow that sends nothing out in batch -- which is every flow that has not been set to batch mode.
    """
    from ampuly.plugins.office_ui.browser_connector import connector_summary

    found: list[tuple[str, str]] = []
    for node in list(getattr(package, "nodes", []) or []):
        summary = connector_summary(node) if isinstance(node, dict) else None
        verify = dict((summary or {}).get("verify", {}) or {})
        if str(verify.get("mode", "") or "") != "batch":
            continue
        ref = str(dict(verify.get("items", {}) or {}).get("from", "") or "")
        found.append((ref.rsplit(".", 1)[-1] if ref.startswith("order.inputs.") else "", str(verify.get("item_key", "") or "")))
    return found


def batch_steps_of(package: Any) -> list[dict[str, Any]]:
    """The batch steps of a flow as the distribution authorization check reads them (D1, policy):
    each step's ``name``, the ``sites`` it works on (its connector's allowed sites, as hosts) and the ``action`` it
    declared (upload / publish / look_back; '' when it declared none). From the same normalised summary the running
    step reads, so launch and every read of the list judge the same thing. Empty for a flow with no batch step."""
    from ampuly.plugins.office_ui.browser_connector import connector_summary

    found: list[dict[str, Any]] = []
    for node in list(getattr(package, "nodes", []) or []):
        summary = connector_summary(node) if isinstance(node, dict) else None
        if str(dict((summary or {}).get("verify", {}) or {}).get("mode", "") or "") != "batch":
            continue
        # The step's name only, never its id: it ends up in the owner's sentence ('' reads 「这一步」).
        found.append({"name": str(node.get("name", "") or "").strip(),
                      "sites": list(summary.get("allowed_domains", []) or []),
                      "action": str(summary.get("distribution_action", "") or "")})
    return found


def batch_identity_text(value: Any) -> str:
    """A name as the kernel keeps it for a row's identity: its first ``ITEM_VALUE_MAX`` characters, stripped."""
    from ampuly.computer_use.browser_bridge import ITEM_VALUE_MAX

    return str(value)[:ITEM_VALUE_MAX].strip()


def _kernel_row(row: dict[str, Any], *, cut: bool = True) -> dict[str, str]:
    """A row flattened the way the kernel flattens it (browser_bridge.order_items): field names cut to
    ``ITEM_FIELD_MAX``, plain values made text (cut to ``ITEM_VALUE_MAX`` unless ``cut`` is False), anything else
    dropped. The constants are the kernel's, imported, so the two can never drift apart."""
    from ampuly.computer_use.browser_bridge import ITEM_FIELD_MAX, ITEM_VALUE_MAX

    return {str(k)[:ITEM_FIELD_MAX]: (str(v)[:ITEM_VALUE_MAX] if cut else str(v)) for k, v in row.items()
            if not isinstance(v, (dict, list, bool)) and v is not None}


def batch_item_identity(row: Any, key: str) -> str:
    """One row's identity exactly as a batch step reads it (browser_bridge.order_items, then the step's
    ``item_key``, stripped): '' when the row has none."""
    if not isinstance(row, dict) or not key:
        return ""
    return str(_kernel_row(row).get(key, "") or "").strip()


def _whole_name(row: dict[str, Any], key: str) -> str:
    """The row's name before the kernel's cut (stripped): what tells two rows apart that the cut made one."""
    return str(_kernel_row(row, cut=False).get(key, "") or "").strip()


def batch_identity_collision(rows: list[Any], key: str, *, seen: dict[str, str] | None = None) -> str:
    """The identity two different rows share once cut the kernel's way, or '' when there is none.

    Two rows with the very same name are one row said twice (kept as it always was); two names that differ only past
    the cut would be ONE row to the batch step, and one authorization line would cover both. ``seen`` carries the
    names across several lists (one authorization names the rows of all of them)."""
    whole = seen if seen is not None else {}
    for row in rows:
        identity = batch_item_identity(row, key)
        if not identity:
            continue
        name = _whole_name(row, key)
        if whole.setdefault(identity, name) != name:
            return identity
    return ""


def read_batch_item_keys(order: Any, lists: list[tuple[str, str]]) -> tuple[list[str] | None, dict[str, str]]:
    """The identities of the rows an order's batch lists carry, in the order's own order, each exactly as the kernel
    names that row -- or None, with why not: ``{"code", "why", "fix"}`` in the owner's words ({} when read).

    LP2-A: the kernel keeps at most ``ORDER_ITEMS_LIMIT`` rows of a list and at most ``ITEM_VALUE_MAX`` characters
    of a value, and a batch step finds its row by that cut value. An authorization names rows the same way, or a
    longer name could never be authorized. Nothing is guessed: a list the batch step would refuse, or one where two
    rows become one once cut, is refused here too (fail closed).
    """
    from ampuly.computer_use.browser_bridge import ITEM_VALUE_MAX, ORDER_ITEMS_LIMIT

    unreadable = {"code": "unreadable", "why": "清单读不出来（缺清单，或某一行没有编号、格式不对）", "fix": "在工单里补齐清单"}
    fields = dict(getattr(order, "fields", {}) or {})
    keys: list[str] = []
    seen: dict[str, str] = {}
    shared = ""
    for name, key in lists:
        rows = fields.get(name) if name else None
        if not key or not isinstance(rows, list) or not rows:
            return None, unreadable
        if len(rows) > ORDER_ITEMS_LIMIT:
            return None, {"code": "too_many", "why": f"清单有 {len(rows)} 条，超过了批量步骤一次最多能做的 {ORDER_ITEMS_LIMIT} 条",
                          "fix": f"把清单分到几张工单里（每张最多 {ORDER_ITEMS_LIMIT} 条）"}
        for row in rows:
            if not isinstance(row, dict):
                return None, unreadable
            value = batch_item_identity(row, key)
            if not value:
                return None, unreadable
            if value not in keys:
                keys.append(value)
        shared = shared or batch_identity_collision(rows, key, seen=seen)
    if shared:
        return None, {"code": "collision",
                      "why": (f"清单里有几条的编号前 {ITEM_VALUE_MAX} 个字一模一样（「{shared[:20]}」开头），"
                              f"批量步骤只认编号的前 {ITEM_VALUE_MAX} 个字，分不清它们"),
                      "fix": f"在工单里把这几条的编号改得前 {ITEM_VALUE_MAX} 个字就不一样"}
    return keys, {}


def batch_item_keys(order: Any, lists: list[tuple[str, str]]) -> list[str] | None:
    """The identities of the rows an order's batch lists carry, in the order's own order -- or None when a list
    cannot be read (the batch step itself refuses such a list; nothing is guessed here). See read_batch_item_keys."""
    return read_batch_item_keys(order, lists)[0]


class ConsoleAPI:
    """Business-layer operations shared by the console, the CLI bridge and tests."""

    # Direct construction (and __new__-built test hosts) must answer the bound
    # launch guard with its domain error, not AttributeError.
    employee_runtime_host: Any | None = None

    def __init__(
        self,
        *,
        ampuly_home: Path | str,
        packages: Callable[[], list[WorkflowPackage]],
        launch: LaunchFn,
        business: BusinessStore | None = None,
        provider_resolver: Callable[[str], Any | None] | None = None,
        skill_library_root: Path | str | None = None,
        acceptance_rework: AcceptanceReworkFn | None = None,
        harness_registry: Any | None = None,
        model_plan_source: Callable[[], Any] | None = None,
        employee_runtime_host: Any | None = None,
        runtime_publication_host: Any | None = None,
    ) -> None:
        self.ampuly_home = Path(ampuly_home)
        self.business = business or BusinessStore(self.ampuly_home)
        self._packages = packages
        self._launch = launch
        self._provider_resolver = provider_resolver
        self._acceptance_rework = acceptance_rework
        self._harness_registry = harness_registry
        # Where the settings an order is frozen under are read from. Injected, like the provider resolver: this
        # layer does not know the engine, and the plan must be built from the live config at the claim.
        self._model_plan_source = model_plan_source
        self.employee_runtime_host = employee_runtime_host
        if runtime_publication_host is not None:
            from .runtime_projection_launch import RuntimeProjectionLaunchHost
            if type(runtime_publication_host) is not RuntimeProjectionLaunchHost:
                raise ValueError('runtime_publication_host_invalid')
        self.runtime_publication_host = runtime_publication_host
        self._skill_library_root = (
            Path(skill_library_root).resolve()
            if skill_library_root is not None
            else Path(__file__).resolve().parents[3] / "distribution" / "skills"
        )
        self._launch_dispatch_lock = asyncio.Lock()
        self._acceptance_rework_dispatch_lock = asyncio.Lock()

    # ── packages ──
    def package(self, workflow_id: str) -> WorkflowPackage | None:
        return next((p for p in self._packages() if p.id == str(workflow_id or "").strip()), None)

    @staticmethod
    def _refuse_other_projects_flow(package: WorkflowPackage, project_id: str, *, doing: str = "下单") -> None:
        """One project is one customer: a flow derived for a project is used by that project only (usable_by_project).

        ``package()`` resolves an id across every project, so each entry that acts for a project checks this itself.
        The sentence reaches the owner's console as it is, so it is said in words, not ids.
        """
        if not usable_by_project(package, project_id):
            raise OrderTransitionError(f"这条业务流是另一个项目专用的，不能在本项目{doing}")

    # ── supervised improvement proposals ──
    def improvement_proposals(self, *, project_id: str) -> list[dict[str, Any]]:
        """List safe proposal metadata inside one project's RSI ledger."""

        try:
            return list_improvement_proposals(project_improvement_root(self.ampuly_home, project_id))
        except ImprovementProposalError as exc:
            raise OrderTransitionError(str(exc)) from exc

    def create_improvement(
        self,
        payload: dict[str, Any],
        *,
        actor: str,
        project_id: str,
        conversation_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        """Persist an inert, evidence-bound proposal; never execute or enable it."""

        root = project_improvement_root(self.ampuly_home, project_id)
        try:
            proposal = create_improvement_proposal(
                root,
                payload,
                actor=actor,
                project_id=project_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
            return next(
                row for row in list_improvement_proposals(root)
                if row["proposal_id"] == proposal.proposal_id
            )
        except (ImprovementProposalError, StopIteration) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def create_improvement_from_acceptance(
        self,
        order_id: str,
        *,
        title: str,
        rationale: str,
        candidate: dict[str, Any],
        actor: str,
        project_id: str,
        conversation_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        """Create an inert process proposal from a durable customer rejection.

        The caller supplies the proposed replacement, but cannot choose the
        source event, workflow identity or target fingerprint. Those facts
        come from the project-scoped order, its verified event chain and the
        currently installed exact workflow version.
        """

        order = self.business.get_order(order_id, project_id=project_id)
        if order is None:
            raise OrderTransitionError(f"order {order_id!r} not found")
        acceptance = self.business.acceptance(
            order.order_id, project_id=project_id
        )
        if acceptance is None:
            raise OrderTransitionError("acceptance criteria have not been defined")
        package = self.package(order.workflow_id)
        if package is None:
            raise OrderTransitionError("order workflow package is unavailable")
        installed_version = str(
            package.manifest.get("version", "") or ""
        ).strip()
        if (
            not acceptance.workflow_version
            or acceptance.workflow_version != installed_version
        ):
            raise OrderTransitionError(
                "rejected acceptance does not match the installed workflow version"
            )
        if not self.business.verify_order_event_chain(order.order_id):
            raise OrderTransitionError(
                "order event chain failed integrity verification"
            )
        rejected = [
            event
            for event in self.business.events(order.order_id)
            if event.get("kind") == "acceptance.rejected"
        ]
        if not rejected:
            raise OrderTransitionError(
                "an acceptance-derived improvement requires a customer rejection"
            )
        source = rejected[-1]
        event_id = str(source.get("event_id", "") or "").strip()
        event_hash = str(
            dict(source.get("integrity", {}) or {}).get("event_hash", "") or ""
        ).strip().lower()
        if not event_id or len(event_hash) != 64:
            raise OrderTransitionError(
                "acceptance rejection has no immutable event identity"
            )
        return self.create_improvement(
            {
                "title": title,
                "rationale": rationale,
                "target": {
                    "kind": PROCESS_TARGET_KIND,
                    "id": package.id,
                    "current_fingerprint": package_candidate_fingerprint(package),
                },
                "evidence": [{
                    "kind": "acceptance",
                    "id": event_id,
                    "sha256": event_hash,
                }],
                "candidate": candidate,
            },
            actor=actor,
            project_id=project_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )

    def reject_improvement(
        self,
        proposal_id: str,
        *,
        actor: str,
        project_id: str,
        expected_fingerprint: str,
        note: str,
    ) -> dict[str, Any]:
        """Record an owner rejection; evidence stays immutable and no candidate is made."""

        root = project_improvement_root(self.ampuly_home, project_id)
        try:
            proposal = reject_improvement_proposal(
                improvement_proposal_by_id(root, proposal_id),
                owner_actor=actor,
                expected_fingerprint=expected_fingerprint,
                note=note,
            )
            return next(
                row for row in list_improvement_proposals(root)
                if row["proposal_id"] == proposal.proposal_id
            )
        except (ImprovementProposalError, StopIteration) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def materialize_process_improvement(
        self,
        proposal_id: str,
        *,
        actor: str,
        project_id: str,
        expected_fingerprint: str,
        draft_id: str,
        allow_draft_missing_skills: bool = False,
    ) -> WorkflowPackage:
        """Turn an approved ProcessSpec proposal into a draft-only workflow candidate."""

        root = project_improvement_root(self.ampuly_home, project_id)
        try:
            proposal = improvement_proposal_by_id(root, proposal_id)
            target_id = str(dict(proposal.manifest.get("target", {}) or {}).get("id", ""))
            template = self.package(target_id)
            if template is None:
                raise ImprovementProposalError(f"workflow {target_id!r} not found")
            return materialize_process_candidate(
                proposal,
                template,
                ampuly_home=self.ampuly_home,
                draft_id=draft_id,
                owner_actor=actor,
                expected_fingerprint=expected_fingerprint,
                allow_draft_missing_skills=allow_draft_missing_skills,
            )
        except (ImprovementProposalError, OSError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def materialize_connector_improvement(
        self,
        proposal_id: str,
        *,
        actor: str,
        project_id: str,
        expected_fingerprint: str,
    ) -> ConnectorCandidate:
        """Create a draft replacement only for the authoritative registered fingerprint."""

        proposal_root = project_improvement_root(self.ampuly_home, project_id)
        candidate_root = project_candidate_root(self.ampuly_home, project_id)
        try:
            proposal = improvement_proposal_by_id(proposal_root, proposal_id)
            target_id = str(dict(proposal.manifest.get("target", {}) or {}).get("id", ""))
            registration = registration_by_name(candidate_root / "registry", target_id)
            return materialize_connector_candidate(
                proposal,
                candidate_root,
                owner_actor=actor,
                expected_fingerprint=expected_fingerprint,
                observed_target_fingerprint=str(registration["candidate_fingerprint"]),
            )
        except (ConnectorCandidateError, ImprovementProposalError, OSError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def materialize_skill_improvement(
        self,
        proposal_id: str,
        *,
        actor: str,
        project_id: str,
        expected_fingerprint: str,
    ) -> SkillReplacementCandidate:
        """Create a versioned Skill draft beside, never over, the internal library."""

        proposal_root = project_improvement_root(self.ampuly_home, project_id)
        try:
            proposal = improvement_proposal_by_id(proposal_root, proposal_id)
            target_id = str(dict(proposal.manifest.get("target", {}) or {}).get("id", ""))
            current = (self._skill_library_root / target_id).resolve()
            if current.parent != self._skill_library_root or not current.is_dir() or current.is_symlink():
                raise SkillReplacementError("current skill is not in the internal library")
            return materialize_skill_candidate(
                proposal,
                current,
                project_skill_candidate_root(self.ampuly_home, project_id),
                owner_actor=actor,
                expected_fingerprint=expected_fingerprint,
            )
        except (ImprovementProposalError, OSError, SkillReplacementError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def bind_improvement_evaluation(
        self,
        proposal_id: str,
        *,
        actor: str,
        project_id: str,
        expected_fingerprint: str,
        spec_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Freeze a numeric comparison before either candidate run is accepted."""
        del actor  # authorization is enforced by the caller; the spec itself is content-addressed
        root = project_improvement_root(self.ampuly_home, project_id)
        try:
            proposal = improvement_proposal_by_id(root, proposal_id)
            if proposal.fingerprint != str(expected_fingerprint or "").strip():
                raise ImprovementProposalError("proposal fingerprint changed; reload before binding evaluation")
            bound = bind_improvement_evaluation(
                proposal, freeze_evaluation_spec(spec_payload),
            )
            return next(
                row for row in list_improvement_proposals(root)
                if row["proposal_id"] == bound.proposal_id
            )
        except (ImprovementProposalError, StopIteration, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def record_improvement_evaluation(
        self,
        proposal_id: str,
        *,
        actor: str,
        project_id: str,
        expected_fingerprint: str,
        baseline: dict[str, Any],
        candidate_run: dict[str, Any],
        note: str,
    ) -> dict[str, Any]:
        """Record the frozen dual run without approving or activating its candidate."""
        root = project_improvement_root(self.ampuly_home, project_id)
        try:
            proposal = improvement_proposal_by_id(root, proposal_id)
            if proposal.fingerprint != str(expected_fingerprint or "").strip():
                raise ImprovementProposalError("proposal fingerprint changed; reload before recording evaluation")
            evaluated = record_improvement_evaluation(
                proposal, baseline=baseline, candidate_run=candidate_run,
                actor=actor, note=note,
            )
            return next(
                row for row in list_improvement_proposals(root)
                if row["proposal_id"] == evaluated.proposal_id
            )
        except (ImprovementProposalError, StopIteration, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def bind_improvement_harness_sessions(
        self,
        proposal_id: str,
        *,
        actor: str,
        project_id: str,
        expected_fingerprint: str,
        variant: str,
        session_ids: list[str],
    ) -> dict[str, Any]:
        """Pre-bind a complete Harness sample set before any turn runs."""

        del actor  # the owner-only caller is authenticated at the console boundary
        if self._harness_registry is None:
            raise OrderTransitionError("Harness registry is unavailable")
        root = project_improvement_root(self.ampuly_home, project_id)
        try:
            proposal = improvement_proposal_by_id(root, proposal_id)
            if proposal.fingerprint != str(expected_fingerprint or "").strip():
                raise ImprovementProposalError("proposal fingerprint changed; reload before binding Harness sessions")
            spec = improvement_evaluation_spec(proposal)
            identifiers = [str(value or "").strip() for value in session_ids]
            if len(identifiers) < int(spec.payload["min_samples"]):
                raise ImprovementProposalError("Harness sample set is smaller than the frozen minimum")
            for session_id in identifiers:
                record = self._harness_registry.session(session_id)
                if record is None or str(record.project_id or "") != str(project_id):
                    raise ImprovementProposalError("Harness evaluation session is missing or belongs to another project")
            events = self._harness_registry.bind_evaluation_sessions(
                identifiers,
                spec_fingerprint=spec.fingerprint,
                asset_fingerprint=spec.asset_fingerprint,
                variant=variant,
            )
            return {
                "proposal_id": proposal.proposal_id,
                "proposal_fingerprint": proposal.fingerprint,
                "spec_fingerprint": spec.fingerprint,
                "variant": str(variant or "").strip().lower(),
                "session_ids": [event.session_id for event in events],
            }
        except (ImprovementProposalError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def record_improvement_harness_evaluation(
        self,
        proposal_id: str,
        *,
        actor: str,
        project_id: str,
        expected_fingerprint: str,
        baseline_session_ids: list[str],
        candidate_session_ids: list[str],
        note: str,
    ) -> dict[str, Any]:
        """Derive both sides from durable Harness events, then record the comparison."""

        if self._harness_registry is None:
            raise OrderTransitionError("Harness registry is unavailable")
        root = project_improvement_root(self.ampuly_home, project_id)
        try:
            proposal = improvement_proposal_by_id(root, proposal_id)
            if proposal.fingerprint != str(expected_fingerprint or "").strip():
                raise ImprovementProposalError("proposal fingerprint changed; reload before recording Harness evaluation")
            spec = improvement_evaluation_spec(proposal)
            baseline = harness_evaluation_result(
                self._harness_registry, spec, baseline_session_ids,
                variant="baseline", project_id=project_id,
            )
            candidate = harness_evaluation_result(
                self._harness_registry, spec, candidate_session_ids,
                variant="candidate", project_id=project_id,
            )
            evaluated = record_improvement_evaluation(
                proposal, baseline=baseline, candidate_run=candidate,
                actor=actor, note=note,
            )
            return next(
                row for row in list_improvement_proposals(root)
                if row["proposal_id"] == evaluated.proposal_id
            )
        except (ImprovementProposalError, StopIteration, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    # ── 4.7-A11: 提一个改进 from a verified asset; the approval is judged on real orders ──
    def _improvement_row(self, project_id: str, proposal_id: str) -> dict[str, Any]:
        root = project_improvement_root(self.ampuly_home, project_id)
        return next(row for row in list_improvement_proposals(root) if row["proposal_id"] == proposal_id)

    def _project_orders_on(self, project_id: str, workflow_ids: set[str]) -> list[WorkOrder]:
        from ampuly.plugins.office_ui.business_store import ORDER_LIST_HARD_CAP

        return [
            order for order in self.business.list_orders(project_id=project_id, limit=ORDER_LIST_HARD_CAP)
            if order.workflow_id in workflow_ids
        ]

    def start_improvement(
        self,
        target_kind: str,
        target_id: str,
        request: str,
        *,
        actor: str,
        project_id: str,
        conversation_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        """One sentence on a published flow, a library skill or a registered connector -> an inert candidate.

        The server drafts the whole proposal from the asset as it is now (its fingerprint, its verified release and
        the recent orders it ran) and materializes it through the same governed writers as every other proposal. The
        answer names the editor the candidate opens in. Nothing is tested, approved, staged, registered or published.
        """
        kind = str(target_kind or "").strip().lower()
        kind = "process" if kind in {PROCESS_TARGET_KIND, "flow"} else kind
        target = str(target_id or "").strip()
        if not target:
            raise OrderTransitionError("先选要改进的对象。")
        try:
            if kind == "process":
                return self._start_process_improvement(
                    target, request, actor=actor, project_id=project_id,
                    conversation_id=conversation_id, turn_id=turn_id,
                )
            if kind == "skill":
                return self._start_skill_improvement(
                    target, request, actor=actor, project_id=project_id,
                    conversation_id=conversation_id, turn_id=turn_id,
                )
            if kind == "connector":
                return self._start_connector_improvement(
                    target, request, actor=actor, project_id=project_id,
                    conversation_id=conversation_id, turn_id=turn_id,
                )
        except (ImprovementProposalError, ConnectorCandidateError, SkillReplacementError, OSError, ValueError) as exc:
            if isinstance(exc, OrderTransitionError):
                raise
            raise OrderTransitionError(str(exc)) from exc
        raise OrderTransitionError("改进对象只能是流程、技能或连接器。")

    def _start_process_improvement(
        self, target: str, request: str, *, actor: str, project_id: str, conversation_id: str, turn_id: str,
    ) -> dict[str, Any]:
        package = self.package(target)
        if package is None or not usable_by_project(package, project_id):
            raise OrderTransitionError("找不到这条流程，或者它是另一个项目专用的。")
        if package.status != "published":
            raise OrderTransitionError("只有已发布的流程能提改进；草稿和测试中的流程直接在搭建台改。")
        candidate, not_carried = draft_process_payload(package, request, author=actor)
        evidence = order_run_evidence(self.business, self._project_orders_on(project_id, {package.id}))
        row = self.create_improvement(
            improvement_request_payload(
                "process", package.id, package_candidate_fingerprint(package), request, candidate,
                asset_version=str(package.manifest.get("version", "") or ""), order_evidence=evidence,
            ),
            actor=actor, project_id=project_id, conversation_id=conversation_id, turn_id=turn_id,
        )
        # One draft per proposal: asking again recovers the same draft instead of making another.
        draft_id = f"{package.id}-improve-{row['proposal_fingerprint'][:8]}"
        draft = self.materialize_process_improvement(
            row["proposal_id"], actor=actor, project_id=project_id,
            expected_fingerprint=row["proposal_fingerprint"], draft_id=draft_id,
        )
        return {
            "proposal": self._improvement_row(project_id, row["proposal_id"]),
            "editor": {"kind": "process", "workflow_id": draft.id, "name": draft.name},
            "not_carried": not_carried,
        }

    def _start_skill_improvement(
        self, target: str, request: str, *, actor: str, project_id: str, conversation_id: str, turn_id: str,
    ) -> dict[str, Any]:
        from ampuly.plugins.office_ui.skill_replacement import skill_tree_fingerprint
        from ampuly.plugins.office_ui.workflow_packages import load_skill_contract

        current = (self._skill_library_root / target).resolve()
        if current.parent != self._skill_library_root or not current.is_dir() or current.is_symlink():
            raise OrderTransitionError("内部库里没有这个技能。")
        candidate = draft_skill_payload(current, request)
        flows = {
            package.id for package in self._packages()
            if usable_by_project(package, project_id) and target in list(package.skills or [])
        }
        evidence = order_run_evidence(self.business, self._project_orders_on(project_id, flows))
        row = self.create_improvement(
            improvement_request_payload(
                "skill", target, skill_tree_fingerprint(current), request, candidate,
                asset_version=load_skill_contract(current.parent, current.name).version, order_evidence=evidence,
            ),
            actor=actor, project_id=project_id, conversation_id=conversation_id, turn_id=turn_id,
        )
        made = self.materialize_skill_improvement(
            row["proposal_id"], actor=actor, project_id=project_id, expected_fingerprint=row["proposal_fingerprint"],
        )
        return {
            "proposal": self._improvement_row(project_id, row["proposal_id"]),
            "editor": {"kind": "skill", "skill_id": target, "candidate_id": made.candidate_id},
            "not_carried": [],
        }

    def _start_connector_improvement(
        self, target: str, request: str, *, actor: str, project_id: str, conversation_id: str, turn_id: str,
    ) -> dict[str, Any]:
        from ampuly.plugins.office_ui.connector_candidate import candidate_by_id

        root = project_candidate_root(self.ampuly_home, project_id)
        try:
            registration = registration_by_name(root / "registry", target)
        except ConnectorCandidateError as exc:
            raise OrderTransitionError("只有已注册的连接器能提改进。") from exc
        if registration["status"] != "registered":
            raise OrderTransitionError("只有已注册的连接器能提改进。")
        current = candidate_by_id(root, registration["candidate_id"])
        candidate = draft_connector_payload(current.contract.to_payload(), request)
        row = self.create_improvement(
            improvement_request_payload(
                "connector", target, registration["candidate_fingerprint"], request, candidate,
                asset_version=registration["version"],
            ),
            actor=actor, project_id=project_id, conversation_id=conversation_id, turn_id=turn_id,
        )
        made = self.materialize_connector_improvement(
            row["proposal_id"], actor=actor, project_id=project_id, expected_fingerprint=row["proposal_fingerprint"],
        )
        return {
            "proposal": self._improvement_row(project_id, row["proposal_id"]),
            "editor": {"kind": "connector", "connector": target, "candidate_id": made.candidate_id},
            "not_carried": [],
        }

    def _benefit_basis(self, proposal: Any, project_id: str) -> tuple[set[str], set[str], str, str, str]:
        """Which orders are the old version and which the new: (old flows, new flows, switched at, until, in words)."""
        from ampuly.plugins.office_ui.skill_activation import list_skill_activations, project_skill_activation_root

        target = dict(proposal.manifest.get("target", {}) or {})
        kind = str(target.get("kind", "") or "")
        target_id = str(target.get("id", "") or "")
        candidate_id = str(proposal.manifest.get("candidate_id", "") or "")
        usable = [package for package in self._packages() if usable_by_project(package, project_id)]
        if kind == PROCESS_TARGET_KIND:
            return ({target_id}, {candidate_id} if candidate_id else set(), "", "",
                    "旧版本是现在发布的流程，新版本是改进草稿：草稿提交测试后下的真实订单才算新版本的样本。")
        if kind == "skill":
            flows = {package.id for package in usable if target_id in list(package.skills or [])}
            activation = next((
                item for item in list_skill_activations(project_skill_activation_root(self.ampuly_home, project_id))
                if str(item.manifest.get("candidate_id", "")) == candidate_id
            ), None)
            switched = str(activation.manifest.get("activated_at", "") or "") if activation is not None else ""
            if not switched:
                return flows, set(), "", "", "新版本还没启用，还没有用新版本跑过的订单。"
            until = str(activation.manifest.get("rolled_back_at", "") or "")
            return flows, flows, switched, until, "按用到这个技能的流程，以新版本启用的时间为界。"
        if kind == "connector":
            prefix = f"connector.{target_id}."
            flows = {
                package.id for package in usable
                if any(
                    str(tool) == target_id or str(tool).startswith(prefix)
                    for node in list(package.nodes or []) if isinstance(node, dict)
                    for tool in list(node.get("tools") or [])
                )
            } or {package.id for package in usable}
            try:
                registration = registration_by_name(project_candidate_root(self.ampuly_home, project_id) / "registry", target_id)
            except ConnectorCandidateError:
                registration = {}
            if str(registration.get("candidate_id", "")) != candidate_id or not registration.get("registered_at"):
                return flows, set(), "", "", "新版本还没注册，还没有用新版本跑过的订单。"
            return flows, flows, str(registration["registered_at"]), "", "以新版本注册的时间为界。"
        return set(), set(), "", "", ""

    def improvement_benefit_report(
        self,
        proposal_id: str,
        *,
        project_id: str,
        sample_size: int = DEFAULT_BENEFIT_SAMPLE,
        spend: dict[str, float] | None = None,
        threshold: float = DEFAULT_WORSE_THRESHOLD,
    ) -> dict[str, Any]:
        """The last N finished real orders before and after, compared; nothing here is typed by a person."""
        root = project_improvement_root(self.ampuly_home, project_id)
        proposal = improvement_proposal_by_id(root, proposal_id)
        baseline_flows, candidate_flows, switched, until, basis = self._benefit_basis(proposal, project_id)
        orders = self._project_orders_on(project_id, baseline_flows | candidate_flows)
        facts = [order_benefit_fact(self.business, order, spend=spend) for order in orders]
        baseline, candidate = select_benefit_samples(
            facts, baseline_workflows=baseline_flows, candidate_workflows=candidate_flows,
            switch_at=switched, until=until, sample_size=sample_size,
        )
        return judge_order_benefit(baseline, candidate, sample_size=sample_size, threshold=threshold, basis=basis)

    def _live_candidate_fingerprint(self, proposal: Any, project_id: str) -> str:
        """The proposal's candidate as it is on disk now (LP-B P-g); '' when it cannot be read.

        A flow draft is fingerprinted as the package it is now (package_candidate_fingerprint); a skill or connector
        candidate by the fingerprint its own store keeps, loaded through its checks, so one whose files changed does
        not load and reads ''. Kept with each benefit evaluation and compared with it before a decision; a skill or
        connector candidate is also compared with the value the proposal was made with (decide_improvement_proposal).
        """
        from ampuly.plugins.office_ui.connector_candidate import candidate_by_id

        kind = str(proposal.manifest.get("candidate_kind", "") or "")
        candidate_id = str(proposal.manifest.get("candidate_id", "") or "").strip()
        if not candidate_id:
            return ""
        try:
            if kind == "process_spec":
                package = self.package(candidate_id)
                return package_candidate_fingerprint(package) if package is not None else ""
            if kind == "skill_contract":
                return skill_replacement_by_id(project_skill_candidate_root(self.ampuly_home, project_id), candidate_id).fingerprint
            if kind == "connector_contract":
                return candidate_by_id(project_candidate_root(self.ampuly_home, project_id), candidate_id).fingerprint
        except (ConnectorCandidateError, SkillReplacementError, OSError, ValueError):
            return ""
        return ""

    def evaluate_improvement_benefit(
        self,
        proposal_id: str,
        *,
        actor: str,
        project_id: str,
        expected_fingerprint: str,
        sample_size: int = DEFAULT_BENEFIT_SAMPLE,
        spend: dict[str, float] | None = None,
        threshold: float = DEFAULT_WORSE_THRESHOLD,
    ) -> dict[str, Any]:
        """Record the real-order comparison on the proposal (it can be asked again as more orders finish)."""
        root = project_improvement_root(self.ampuly_home, project_id)
        try:
            proposal = improvement_proposal_by_id(root, proposal_id)
            if proposal.fingerprint != str(expected_fingerprint or "").strip():
                raise ImprovementProposalError("proposal fingerprint changed; reload before evaluating")
            report = self.improvement_benefit_report(
                proposal_id, project_id=project_id, sample_size=sample_size, spend=spend, threshold=threshold,
            )
            # Kept under the candidate as it is now, not a copy of the value written at materialize (LP-B P-g).
            record_improvement_benefit(
                proposal, report, actor=actor,
                candidate_fingerprint=self._live_candidate_fingerprint(proposal, project_id),
            )
            return self._improvement_row(project_id, proposal_id)
        except (ImprovementProposalError, StopIteration, ValueError) as exc:
            if isinstance(exc, OrderTransitionError):
                raise
            raise OrderTransitionError(str(exc)) from exc

    def approve_improvement(
        self,
        proposal_id: str,
        *,
        actor: str,
        project_id: str,
        expected_fingerprint: str,
        note: str = "",
        sample_size: int = DEFAULT_BENEFIT_SAMPLE,
        spend: dict[str, float] | None = None,
        threshold: float = DEFAULT_WORSE_THRESHOLD,
    ) -> dict[str, Any]:
        """Judge the benefit on the orders as they are now, then decide: worse refuses, too few decides nothing.

        Returns ``approved`` and the reason in words; a refusal for negative benefit is recorded (the proposal is
        rejected), a refusal for too few orders raises and leaves the proposal open for more orders.
        """
        root = project_improvement_root(self.ampuly_home, project_id)
        try:
            proposal = improvement_proposal_by_id(root, proposal_id)
            if proposal.status == "materialized":
                self.evaluate_improvement_benefit(
                    proposal_id, actor=actor, project_id=project_id, expected_fingerprint=expected_fingerprint,
                    sample_size=sample_size, spend=spend, threshold=threshold,
                )
                proposal = improvement_proposal_by_id(root, proposal_id)
            # LP-B P-g: the candidate as it is on disk right now. One changed since the orders were judged just above
            # (「候选在评估之后被改过，请重新评估」), one that cannot be read, or a skill / connector candidate that is no
            # longer the one the proposal made (「候选在生成改进提案之后被改过或读不出来，不能批准」) is refused and the
            # proposal stays open. A flow draft finished on the builder and submitted for testing approves.
            live = self._live_candidate_fingerprint(proposal, project_id) if proposal.status == "materialized" else None
            _decided, approved, reason = decide_improvement_proposal(
                proposal, owner_actor=actor, expected_fingerprint=expected_fingerprint, note=note,
                live_fingerprint=live,
            )
            return {"approved": approved, "reason": reason, "proposal": self._improvement_row(project_id, proposal_id)}
        except (ImprovementProposalError, StopIteration, ValueError) as exc:
            if isinstance(exc, OrderTransitionError):
                raise
            raise OrderTransitionError(str(exc)) from exc

    def bind_improvement_session_standard(
        self,
        proposal_id: str,
        *,
        actor: str,
        project_id: str,
        expected_fingerprint: str,
        hypothesis: str,
        metrics: list[dict[str, Any]],
        min_samples: Any,
    ) -> dict[str, Any]:
        """Freeze a run-session dual run for a proposal's candidate; what is measured is filled in, never typed."""
        from ampuly.plugins.office_ui.component_versions import DUAL_RUN_METRICS, MAX_SAMPLES

        text = str(hypothesis or "").strip()
        if not text or len(text) > 1000:
            raise OrderTransitionError("写一句新版本应该比旧版本好在哪（1000 字以内）。")
        if not isinstance(metrics, list) or not 1 <= len(metrics) <= len(DUAL_RUN_METRICS):
            raise OrderTransitionError("至少选一项要比较的指标。")
        rows: list[dict[str, Any]] = []
        for item in metrics:
            name = str(item.get("name", "") if isinstance(item, dict) else "").strip()
            if name not in DUAL_RUN_METRICS or any(row["name"] == name for row in rows):
                raise OrderTransitionError("只能比较系统能从运行记录里算出来的指标：完成率、交出产物的比例、失败率、停下来等人的比例，每项一次。")
            try:
                delta = float(item.get("min_delta"))
            except (TypeError, ValueError):
                delta = 0.0
            if not 0 < delta <= 1:
                raise OrderTransitionError("每项指标要求的提升要大于 0、不超过 1（比例，例如 0.1 就是 10 个百分点）。")
            rows.append({"name": name, "direction": DUAL_RUN_METRICS[name], "min_delta": delta, "lower_bound": 0, "upper_bound": 1})
        if isinstance(min_samples, bool) or not isinstance(min_samples, int) or not 1 <= min_samples <= MAX_SAMPLES:
            raise OrderTransitionError(f"每组样本数要是 1 到 {MAX_SAMPLES} 之间的整数。")
        root = project_improvement_root(self.ampuly_home, project_id)
        try:
            proposal = improvement_proposal_by_id(root, proposal_id)
            identity = improvement_asset_identity(proposal)
        except ImprovementProposalError as exc:
            raise OrderTransitionError(str(exc)) from exc
        return self.bind_improvement_evaluation(
            proposal_id, actor=actor, project_id=project_id, expected_fingerprint=expected_fingerprint,
            spec_payload={"asset": identity, "hypothesis": text, "metrics": rows, "min_samples": min_samples},
        )

    # 4.7-A03: a skill candidate's own dual run. The proposal routes above record into the proposal's store, which
    # staging never reads and an owner's edit does not have; these record into the candidate itself, where approval
    # and staging read (evaluation-report.json), from the same pre-bound Harness evidence and nothing typed.
    def bind_skill_improvement_harness_sessions(
        self,
        candidate_id: str,
        *,
        actor: str,
        project_id: str,
        expected_fingerprint: str,
        variant: str,
        session_ids: list[str],
    ) -> dict[str, Any]:
        """Pre-bind one side of a skill candidate's dual run before any of its turns runs."""

        del actor  # the owner-only caller is authenticated at the console boundary
        if self._harness_registry is None:
            raise OrderTransitionError("这台控制台没有接上运行记录，做不了双跑评估。")
        root = project_skill_candidate_root(self.ampuly_home, project_id)
        try:
            candidate = skill_replacement_by_id(root, candidate_id)
            if candidate.fingerprint != str(expected_fingerprint or "").strip():
                raise SkillReplacementError("skill candidate fingerprint changed; reload before binding Harness sessions")
            if candidate.manifest.get("evaluation_report_sha256"):
                raise SkillReplacementError("这个候选已经做过一次双跑评估，结果不能重做；没通过就修订成新候选再测。")
            spec = skill_replacement_evaluation_spec(candidate)
            identifiers = [str(value or "").strip() for value in session_ids]
            if len(identifiers) < int(spec.payload["min_samples"]):
                raise SkillReplacementError(
                    f"这一组只有 {len(identifiers)} 个会话，少于评估标准要求的 {int(spec.payload['min_samples'])} 个。"
                )
            for session_id in identifiers:
                record = self._harness_registry.session(session_id)
                if record is None or str(record.project_id or "") != str(project_id):
                    raise SkillReplacementError("有会话不存在，或属于别的项目。")
            side = str(variant or "").strip().lower()
            registered = _registered_dual_run_sessions(candidate.root)
            if side in registered and sorted(registered[side]) != sorted(identifiers):
                raise SkillReplacementError(
                    "这一组会话已经登记过了；双跑评估的两组会话只能登记一次，不能换。"
                )
            events = self._harness_registry.bind_evaluation_sessions(
                identifiers,
                spec_fingerprint=spec.fingerprint,
                asset_fingerprint=spec.asset_fingerprint,
                variant=variant,
            )
            # The record step must use exactly these sessions: a group chosen after its outcomes are known is not an
            # experiment (review, : a subset could be picked at record time to pass the gate).
            registered[side] = sorted(event.session_id for event in events)
            _write_registered_dual_run_sessions(candidate.root, spec.fingerprint, registered)
            return {
                "candidate_id": candidate.candidate_id,
                "candidate_fingerprint": candidate.fingerprint,
                "spec_fingerprint": spec.fingerprint,
                "variant": str(variant or "").strip().lower(),
                "session_ids": [event.session_id for event in events],
            }
        except (OSError, SkillReplacementError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def record_skill_improvement_harness_evaluation(
        self,
        candidate_id: str,
        *,
        actor: str,
        project_id: str,
        expected_fingerprint: str,
        baseline_session_ids: list[str],
        candidate_session_ids: list[str],
        note: str,
    ) -> SkillReplacementCandidate:
        """Derive both sides from pre-bound Harness events and record them on the candidate; approves nothing."""

        if self._harness_registry is None:
            raise OrderTransitionError("这台控制台没有接上运行记录，做不了双跑评估。")
        root = project_skill_candidate_root(self.ampuly_home, project_id)
        try:
            candidate = skill_replacement_by_id(root, candidate_id)
            if candidate.fingerprint != str(expected_fingerprint or "").strip():
                raise SkillReplacementError("skill candidate fingerprint changed; reload before recording Harness evaluation")
            spec = skill_replacement_evaluation_spec(candidate)
            registered = _registered_dual_run_sessions(candidate.root)
            for side, supplied in (("baseline", baseline_session_ids), ("candidate", candidate_session_ids)):
                wanted = sorted(registered.get(side, []))
                given = sorted(str(value or "").strip() for value in list(supplied or []))
                if not wanted:
                    raise SkillReplacementError("这一组会话还没登记：先登记两组会话、再跑、最后记录。")
                if given != wanted:
                    raise SkillReplacementError(
                        "记录评估时要用当初登记的全部会话，不能增减或换掉其中的会话。"
                    )
            baseline = harness_evaluation_result(
                self._harness_registry, spec, baseline_session_ids,
                variant="baseline", project_id=project_id,
            )
            trial = harness_evaluation_result(
                self._harness_registry, spec, candidate_session_ids,
                variant="candidate", project_id=project_id,
            )
            return record_skill_replacement_evaluation(
                candidate, baseline=baseline, candidate_run=trial, actor=actor,
                note=str(note or "").strip() or "新旧两版各跑一组，由系统从预先登记的运行记录比对",
            )
        except (OSError, SkillReplacementError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def record_skill_improvement_test(
        self,
        candidate_id: str,
        *,
        actor: str,
        project_id: str,
        outcome: str,
        note: str,
    ) -> SkillReplacementCandidate:
        """Record a human-run test result; this API never executes candidate instructions."""

        root = project_skill_candidate_root(self.ampuly_home, project_id)
        try:
            return record_skill_replacement_test(
                skill_replacement_by_id(root, candidate_id),
                outcome=outcome,
                actor=actor,
                note=note,
            )
        except (OSError, SkillReplacementError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def review_skill_improvement(
        self,
        candidate_id: str,
        *,
        actor: str,
        project_id: str,
        approved: bool,
        note: str,
        expected_fingerprint: str,
    ) -> SkillReplacementCandidate:
        """Record the owner verdict; approval still does not install or replace the Skill."""

        root = project_skill_candidate_root(self.ampuly_home, project_id)
        try:
            return review_skill_replacement_candidate(
                skill_replacement_by_id(root, candidate_id),
                approved=approved,
                actor=actor,
                note=note,
                expected_fingerprint=expected_fingerprint,
            )
        except (OSError, SkillReplacementError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def stage_skill_improvement(
        self,
        candidate_id: str,
        *,
        actor: str,
        project_id: str,
    ) -> SkillActivation:
        """Seal an approved, evaluated candidate; never activate it implicitly."""

        try:
            candidate = skill_replacement_by_id(
                project_skill_candidate_root(self.ampuly_home, project_id), candidate_id,
            )
            return stage_skill_activation(
                candidate,
                skill_library_root=self._skill_library_root,
                deployment_root=project_skill_activation_root(self.ampuly_home, project_id),
                actor=actor,
            )
        except (OSError, SkillActivationError, SkillReplacementError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def skill_improvements(self, *, project_id: str) -> list[dict[str, Any]]:
        """Return verified review/deployment facts without exposing filesystem evidence."""

        try:
            candidates = list_skill_replacement_candidates(
                project_skill_candidate_root(self.ampuly_home, project_id)
            )
            deployments = {
                str(item.manifest.get("candidate_id", "")): item.to_payload()
                for item in list_skill_activations(
                    project_skill_activation_root(self.ampuly_home, project_id)
                )
            }
            return [
                {**candidate.to_payload(), "activation": deployments.get(candidate.candidate_id)}
                for candidate in candidates
            ]
        except (OSError, SkillActivationError, SkillReplacementError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def activate_skill_improvement(
        self,
        candidate_id: str,
        *,
        actor: str,
        project_id: str,
        expected_fingerprint: str,
    ) -> SkillActivation:
        """Explicitly switch one staged Skill; never combine this with approval."""

        try:
            deployment = skill_activation_by_id(
                project_skill_activation_root(self.ampuly_home, project_id), candidate_id,
            )
            return activate_skill(
                deployment,
                skill_library_root=self._skill_library_root,
                actor=actor,
                expected_fingerprint=expected_fingerprint,
            )
        except (OSError, SkillActivationError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def rollback_skill_improvement(
        self,
        candidate_id: str,
        *,
        actor: str,
        project_id: str,
        expected_fingerprint: str,
    ) -> SkillActivation:
        """Explicitly restore the sealed previous Skill version."""

        try:
            deployment = skill_activation_by_id(
                project_skill_activation_root(self.ampuly_home, project_id), candidate_id,
            )
            return rollback_skill(
                deployment,
                skill_library_root=self._skill_library_root,
                actor=actor,
                expected_fingerprint=expected_fingerprint,
            )
        except (OSError, SkillActivationError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def publish_workflow(self, workflow_id: str, *, actor: str, expected_revision: str = "") -> WorkflowPackage:
        """Promote one tested package through the audited package boundary."""
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        try:
            with observed_package_revision(package, expected_revision):
                return publish_package(package, actor=actor, employee_runtime_host=self.employee_runtime_host)
        except (OSError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def rollback_workflow(self, workflow_id: str, *, actor: str, expected_revision: str = "") -> WorkflowPackage:
        """Restore a published workflow's immutable pre-release snapshot."""
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        try:
            with observed_package_revision(package, expected_revision):
                return rollback_package(package, actor=actor)
        except (OSError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def authorize_workflow_redistribution(self, order_id: str, *, actor: str, project_id: str, note: str = "") -> dict[str, Any]:
        """Record a customer's explicit redistribution grant on their accepted order.

        The caller names the order and nothing else: the workflow, its exact version, candidate fingerprint
        and manifest digest are the published package's own, read the way the asset projection reads them.
        """
        from ampuly.plugins.office_ui.release_runtime_evidence import build_runtime_evidence

        order = self.business.get_order(order_id, project_id=project_id)
        if order is None:
            raise OrderTransitionError(f"order {order_id!r} not found")
        package = self.package(order.workflow_id)
        if package is None:
            raise OrderTransitionError("order workflow package is unavailable")
        if package.status != "published":
            raise OrderTransitionError(f"workflow {package.id!r} is {package.status}, not published; nothing to redistribute")
        published = dict(build_runtime_evidence(package, self.business).get("published_evidence", {}) or {})
        return self.business.authorize_redistribution(
            order.order_id,
            actor=actor,
            workflow_id=package.id,
            version=str(package.manifest.get("version", "") or ""),
            candidate_fingerprint=str(published.get("candidate_fingerprint", "") or ""),
            manifest_sha256=str(published.get("manifest_sha256", "") or ""),
            note=note,
            project_id=project_id,
        )

    def export_workflow_distribution_candidate(self, workflow_id: str, *, actor: str) -> dict[str, Any]:
        """Export one shareable published version as a signed candidate into this home's outbox."""
        from ampuly.plugins.office_ui.distribution_candidate import (
            DistributionCandidateError, export_distribution_candidate, signing_key_from_env,
        )

        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        try:
            return export_distribution_candidate(
                package, self.business, destination_dir=Path(self.ampuly_home) / "distribution-outbox", actor=actor,
                signing_key=signing_key_from_env(),
            )
        except (DistributionCandidateError, OSError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def receive_workflow_distribution_candidate(self, path: str, *, actor: str, decision: str, note: str = "") -> dict[str, Any]:
        """Verify a received candidate and record the owner's decision; an accepted one lands as a draft only."""
        from ampuly.plugins.office_ui.distribution_candidate import (
            DistributionCandidateError, receive_distribution_candidate, signing_key_from_env,
        )

        try:
            return receive_distribution_candidate(
                Path(str(path or "")), ampuly_home=Path(self.ampuly_home), actor=actor, decision=decision, note=note,
                signing_key=signing_key_from_env(),
            )
        except (DistributionCandidateError, OSError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def workflow_distribution_inbox(self) -> list[dict[str, Any]]:
        from ampuly.plugins.office_ui.distribution_candidate import distribution_inbox

        return distribution_inbox(Path(self.ampuly_home))

    def start_workflow_rollout(self, workflow_id: str, *, actor: str, expected_revision: str = "", **policy: Any) -> WorkflowPackage:
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        try:
            with observed_package_revision(package, expected_revision):
                return start_rollout(package, actor=actor, **policy)
        except (OSError, TypeError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def record_workflow_rollout_observation(self, workflow_id: str, *, actor: str, expected_revision: str = "", **observation: Any) -> WorkflowPackage:
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        try:
            with observed_package_revision(package, expected_revision):
                return record_rollout_observation(package, actor=actor, **observation)
        except (OSError, TypeError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def evaluate_workflow_rollout(self, workflow_id: str) -> dict[str, Any]:
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        try:
            return evaluate_rollout(package)
        except (OSError, TypeError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def finalize_workflow_rollout(self, workflow_id: str, *, actor: str, expected_revision: str = "") -> WorkflowPackage:
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        try:
            with observed_package_revision(package, expected_revision):
                return finalize_rollout(package, actor=actor)
        except (OSError, TypeError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def rollback_workflow_after_rollout(self, workflow_id: str, *, actor: str, expected_revision: str = "") -> WorkflowPackage:
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        try:
            with observed_package_revision(package, expected_revision):
                return auto_rollback_package(package, actor=actor)
        except (OSError, TypeError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def record_workflow_test(self, workflow_id: str, *, actor: str, expected_revision: str = "", **kwargs: Any) -> WorkflowPackage:
        """Append an immutable, actor-attributed trial record for Builder use."""
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        try:
            with observed_package_revision(package, expected_revision):
                return record_test_run(package, actor=actor, **kwargs)
        except (OSError, TypeError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def simulate_workflow_test(
        self,
        workflow_id: str,
        fields: dict[str, Any],
        *,
        actor: str,
        expected_revision: str = "",
    ) -> tuple[WorkflowPackage, dict[str, Any]]:
        """Record a contract-only Builder simulation; never launch or send."""
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        try:
            with observed_package_revision(package, expected_revision):
                return simulate_test_run(package, fields, actor=actor)
        except (OSError, TypeError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def create_workflow_draft(
        self,
        workflow_id: str,
        *,
        draft_id: str,
        actor: str,
        name: str = "",
        summary: str = "",
        nodes: list[dict[str, Any]] | None = None,
        intake_fields: list[dict[str, Any]] | None = None,
        acceptance: list[str] | None = None,
        change_note: str = "",
        project_id: str | None = None,
    ) -> WorkflowPackage:
        """Create a local Builder candidate without launching or installing it (the Builder's 「复制为新草稿」).

        The copy has to prove itself and be granted again: it takes none of the source's tool grants, rollout,
        approval records or intake values (draft_manifest_from; owner agreed 「新草稿必须自己证明，放行重新批」).
        ``project_id`` is the project asking: another project's own flow is not copied from it. The copy belongs to
        whoever its source belongs to; ``None`` is a caller answering no project and checks nothing.
        """
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        if project_id is not None:
            self._refuse_other_projects_flow(package, project_id, doing="复制")
        try:
            return create_workflow_draft(
                package,
                ampuly_home=self.ampuly_home,
                draft_id=draft_id,
                actor=actor,
                name=name,
                summary=summary,
                nodes=nodes,
                intake_fields=intake_fields,
                acceptance=acceptance,
                change_note=change_note,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def derive_workflow_for_project(self, workflow_id: str, *, project_id: str, actor: str) -> tuple[WorkflowPackage, bool]:
        """派生为本项目草稿 (policy): a draft of a global flow that this project owns. Returns (draft, existing).

        The template is untouched and stays what other projects adopt. A project gets one derived draft per template:
        asking again opens the one already made. A flow that already belongs to a project is not derived again --
        its project changes it directly. Nothing is adopted, run or published. The draft takes none of the template's
        tool grants, rollout, approval records or intake values (4.7 Q01: 不复制旧授权; see draft_manifest_from), and
        no other project lists, adopts, orders on or runs it (usable_by_project).
        """
        clean_project = str(project_id or "").strip()
        if not clean_project:
            raise OrderTransitionError("a project is required to derive a draft for it")
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        if str(package.manifest.get("project_id", "") or "").strip():
            raise OrderTransitionError("this flow already belongs to a project; derive from the global flow it came from")
        if package.status == "retired":
            raise OrderTransitionError("a retired flow is not derived")
        packages = self._packages()
        for candidate in packages:
            source = candidate.manifest.get("derived_from")
            if (str(candidate.manifest.get("project_id", "") or "").strip() == clean_project
                    and isinstance(source, dict) and str(source.get("id", "") or "") == package.id):
                return candidate, True
        draft_id = derived_draft_id(package.id, clean_project, {item.id for item in packages})
        try:
            derived = create_workflow_draft(
                package,
                ampuly_home=self.ampuly_home,
                draft_id=draft_id,
                actor=actor,
                name=f"{package.name}（{clean_project}）",
                change_note=f"derived for project {clean_project} from {package.id} v{package.manifest.get('version', '')}",
                project_id=clean_project,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc
        return derived, False

    def create_blank_workflow_draft(
        self,
        *,
        draft_id: str,
        actor: str,
        org_source: Path,
        name: str = "",
        summary: str = "",
        intake_fields: list[dict[str, Any]] | None = None,
        created_from: str = "",
    ) -> WorkflowPackage:
        """Create an empty local draft run by one saved team; nothing is launched or installed."""
        from ampuly.plugins.office_ui.workflow_packages import create_blank_workflow_draft

        if self.package(str(draft_id or "").strip().lower()) is not None:
            raise OrderTransitionError(f"workflow {draft_id!r} already exists")
        try:
            return create_blank_workflow_draft(
                ampuly_home=self.ampuly_home, draft_id=draft_id, actor=actor, org_source=org_source, name=name, summary=summary,
                intake_fields=intake_fields, created_from=created_from,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    # ── 项目采用业务流 (4.7 Q01) ──
    def workflow_adoptions(self, project_id: str) -> dict[str, Any]:
        return {
            "adoptions": self.business.workflow_adoptions(project_id),
            "events": self.business.workflow_adoption_events(project_id, limit=100),
        }

    def workflow_versions(self, workflow_id: str) -> dict[str, Any]:
        """流程版本面板: the flow's published versions and which project runs which (workflow_versions.py). Read-only."""
        from ampuly.plugins.office_ui.workflow_versions import version_record

        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        return version_record(package, self.business.workflow_adoptions_of(package.id))

    def adopt_workflow(self, project_id: str, workflow_id: str, *, actor: str, note: str = "") -> dict[str, Any]:
        """Adopt the published version of a flow into a project (again, with a newer one, to upgrade).

        A flow derived for another project is that customer's and is refused here, published or not.
        """
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        self._refuse_other_projects_flow(package, project_id, doing="采用")
        if package.status != "published":
            raise OrderTransitionError(f"only a published flow can be adopted; {package.id!r} is {package.status}")
        try:
            return self.business.adopt_workflow(
                project_id, package.id,
                version=str(package.manifest.get("version", "") or ""),
                fingerprint=str(package.release_evidence.get("candidate_fingerprint", "") or ""),
                actor=actor, note=note,
            )
        except ValueError as exc:
            raise OrderTransitionError(str(exc)) from exc

    def set_workflow_adoption_status(self, project_id: str, workflow_id: str, status: str, *, actor: str, note: str = "") -> dict[str, Any]:
        try:
            return self.business.set_adoption_status(project_id, workflow_id, status, actor=actor, note=note)
        except ValueError as exc:
            raise OrderTransitionError(str(exc)) from exc

    def submit_workflow_for_testing(self, workflow_id: str, *, actor: str, change_note: str = "", expected_revision: str = "") -> WorkflowPackage:
        """Promote a draft with a passing immutable test into testing state."""
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        try:
            with observed_package_revision(package, expected_revision):
                return submit_for_testing(package, actor=actor, change_note=change_note)
        except (OSError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def request_workflow_release(self, workflow_id: str, *, actor: str, note: str = "", expected_revision: str = "") -> WorkflowPackage:
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        try:
            with observed_package_revision(package, expected_revision):
                return request_release_approval(package, actor=actor, note=note,
                    employee_runtime_host=self.employee_runtime_host)
        except (OSError, TypeError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def decide_workflow_release(self, workflow_id: str, *, actor: str, decision: str, note: str = "", expected_revision: str = "") -> WorkflowPackage:
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        try:
            with observed_package_revision(package, expected_revision):
                return decide_release_approval(package, actor=actor, decision=decision, note=note)
        except (OSError, TypeError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def update_workflow_nodes(
        self,
        workflow_id: str,
        nodes: list[dict[str, Any]],
        *,
        actor: str,
        expected_version: str = "",
        new_version: str = "",
        change_note: str = "",
        expected_revision: str = "",
    ) -> WorkflowPackage:
        """Update a draft/testing node table through the shared API boundary."""
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        try:
            return update_package_nodes(
                package,
                nodes,
                actor=actor,
                expected_version=expected_version,
                new_version=new_version,
                change_note=change_note,
                expected_revision=expected_revision,
            )
        except (OSError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def review_skill(self, workflow_id: str, skill_id: str, *, actor: str, note: str = "", expected_revision: str = "") -> WorkflowPackage:
        """Record that a person reviewed a drafted skill of a draft/testing package."""
        from ampuly.plugins.office_ui.skill_factory import mark_skill_reviewed

        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        try:
            with observed_package_revision(package, expected_revision):
                return mark_skill_reviewed(package, skill_id, actor=actor, note=note)
        except (OSError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    def preflight_workflow(
        self,
        workflow_id: str,
        fields: dict[str, Any],
        *,
        project_id: str,
        focus_node_id: str = "",
        default_model: str = "",
    ) -> dict[str, Any]:
        """Return a read-only, simulated Builder plan for one workflow.

        A flow derived for another project is refused here too: the plan lays out that customer's steps and fields,
        and 安安's preflight tool calls this directly, past the console handler's own check.
        """
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        self._refuse_other_projects_flow(package, project_id, doing="预检")
        result = preflight_invocation(package, fields, focus_node_id=focus_node_id, default_model=default_model)
        result["project_id"] = str(project_id or "default").strip() or "default"
        return result

    def provider_trial_preflight(
        self,
        workflow_id: str,
        fields: dict[str, Any],
        *,
        project_id: str,
        provider: str,
    ) -> dict[str, Any]:
        """Join workflow preflight with Provider Conformance without execution.

        A flow derived for another project is refused here as in ``preflight_workflow``: the plan lays out that
        customer's steps, and this entry resolves the id across every project.
        """
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        self._refuse_other_projects_flow(package, project_id, doing="预检")
        if self._provider_resolver is None:
            raise OrderTransitionError("provider resolver is unavailable")
        adapter = self._provider_resolver(str(provider or "").strip())
        if adapter is None:
            raise OrderTransitionError(f"provider {provider!r} is unavailable")
        try:
            return provider_trial_preflight(
                package,
                dict(fields or {}),
                adapter=adapter,
                project_id=project_id,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise OrderTransitionError(str(exc)) from exc

    # ── orders ──
    def _acceptance_payload(
        self,
        order_id: str,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        acceptance = self.business.acceptance(order_id, project_id=project_id)
        if acceptance is None:
            return None
        payload = acceptance.to_payload()
        pending = self.business.pending_acceptance_rework_command(
            order_id,
            request_id=acceptance.rework_request_id,
            project_id=project_id,
        )
        if pending is not None:
            payload["runtime_rework"] = {
                "state": "pending",
                "request_id": str(
                    pending.payload.get("request_id", "") or ""
                ),
                "target_node": str(
                    pending.payload.get("target_node", "") or ""
                ),
                "attempt_count": pending.attempt_count,
                "last_error": pending.last_error,
            }
        return payload

    def _order_payload(self, order: WorkOrder) -> dict[str, Any]:
        payload = order.to_payload()
        payload["acceptance"] = self._acceptance_payload(
            order.order_id,
            project_id=order.project_id,
        )
        control_plane = self.business.fde_control_plane(order.order_id, project_id=order.project_id)
        payload["fde_control_plane"] = control_plane.to_payload() if control_plane else None
        # Who handles it, by when, for how much: the order's own fact, lifted out of `fields` so a page need not
        # know where it is kept.  None when nothing was ever set -- not an object of empty values.
        handling = order.fields.get(HANDLING_KEY) if isinstance(order.fields, dict) else None
        payload["handling"] = dict(handling) if isinstance(handling, dict) and handling else None
        return payload

    def orders(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        customer_id: str | None = None,
        environment_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """The newest orders, at most ``limit`` of them (left out: the store's own default).

        Never more than that, and nothing says whether there were more: a caller that must know asks for one row
        past what it shows and looks at whether it came back.
        """
        query: dict[str, Any] = {
            "project_id": project_id,
            "status": status,
            "customer_id": customer_id,
            "environment_id": environment_id,
        }
        if limit is not None:
            query["limit"] = int(limit)
        return [self._order_payload(o) for o in self.business.list_orders(**query)]

    def orders_page(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        customer_id: str | None = None,
        environment_id: str | None = None,
        limit: int = 200,
        before: str | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """4.7-A08: one page of orders and the cursor of the next (``''`` = nothing older). See ``list_orders_page``."""
        page, next_cursor = self.business.list_orders_page(
            project_id=project_id, status=status, customer_id=customer_id, environment_id=environment_id,
            limit=int(limit), before=before,
        )
        return [self._order_payload(o) for o in page], next_cursor

    def order(
        self,
        order_id: str,
        *,
        project_id: str | None = None,
        customer_id: str | None = None,
        environment_id: str | None = None,
    ) -> dict[str, Any] | None:
        found = self.business.get_order(order_id, project_id=project_id)
        if found is None:
            return None
        if customer_id is not None and found.customer_id != str(customer_id or "").strip():
            return None
        if environment_id is not None and found.environment_id != str(environment_id or "").strip():
            return None
        payload = self._order_payload(found)
        payload["events"] = self.business.events(order_id)
        payload["events_chain_verified"] = self.business.verify_order_event_chain(order_id)
        return payload

    def update_fde_control_plane(
        self,
        order_id: str,
        patch: dict[str, Any],
        *,
        actor: str,
        project_id: str = "default",
    ) -> WorkOrder:
        """Update editable FDE planning fields in the business truth store."""
        return self.business.update_fde_control_plane(
            order_id,
            patch,
            actor=actor,
            project_id=project_id,
        )

    def update_order_handling(
        self,
        order_id: str,
        patch: dict[str, Any],
        *,
        actor: str,
        project_id: str = "default",
    ) -> WorkOrder:
        """Set an order's assignee, due date, amounts or handover note (one signed event per change)."""
        return self.business.update_order_handling(order_id, patch, actor=actor, project_id=project_id)

    def record_observation(self, **kwargs: Any) -> dict[str, Any]:
        """Record a scope-bound read at the console boundary."""
        return self.business.record_observation(**kwargs).to_payload()

    def observations(
        self,
        *,
        project_id: str,
        customer_id: str | None = None,
        environment_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return [
            item.to_payload()
            for item in self.business.observations(
                project_id=project_id,
                customer_id=customer_id,
                environment_id=environment_id,
                limit=limit,
            )
        ]

    def create_order(
        self,
        *,
        workflow_id: str,
        fields: dict[str, Any],
        project_id: str = "default",
        customer_id: str = "",
        environment_id: str = "",
        source: str = "console",
        source_task_id: str = "",
        invocation_key: str = "",
        actor: str = "",
        status: str = "pending_review",
    ) -> WorkOrder:
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        # A flow derived for one project is that customer's: no other project places an order on it. Checked before the
        # adoption, because "never adopted" below is the old global path and must not become a way in.
        self._refuse_other_projects_flow(package, project_id)
        # 项目采用业务流 (4.7 Q01): a project that adopted this flow takes new orders only while the adoption is active
        # and only on the version it adopted. A flow the project never adopted keeps the old global path.
        adoption = self.business.workflow_adoption(project_id, package.id)
        if adoption is not None:
            if adoption["status"] != "active":
                raise OrderTransitionError(
                    f"project {project_id!r} is not taking new orders on {package.id!r} ({adoption['status']})"
                )
            available = str(package.manifest.get("version", "") or "").strip()
            if adoption["workflow_version"] != available:
                raise OrderTransitionError(
                    f"project {project_id!r} adopted {package.id!r} {adoption['workflow_version']}, but {available or 'no version'} "
                    "is what can run now; upgrade the adoption first"
                )
        scoped_fields = dict(fields or {})
        if not str(invocation_key or "").strip():
            # The parent contract is written by the node that invokes a child flow, and only that path names an
            # invocation key. Anything else carrying one -- a console launch, an intake form -- is a claim typed by
            # a client: it would attach the order to another order's task, artifacts and allowance.
            scoped_fields.pop("_workflow_parent", None)
        if (
            not str(customer_id or "").strip()
            and not str(scoped_fields.get("customer_id", "") or "").strip()
            and not str(invocation_key or "").strip()
        ):
            # A project bound to a customer is that customer's delivery space: an order placed inside it is theirs.
            # The console's own launch names no customer, so without this its accepted delivery could never be
            # exported for the project's customer. A node-triggered order carries its parent's scope and is left alone.
            try:
                customer_id = str(self.business.project_customer(project_id) or "").strip()
            except Exception:  # noqa: BLE001 - an unreadable binding is no binding; the order is still placed
                customer_id = ""
        if str(customer_id or "").strip():
            requested_customer = str(customer_id).strip()
            existing_customer = str(scoped_fields.get("customer_id", "") or "").strip()
            if existing_customer and existing_customer != requested_customer:
                raise OrderTransitionError("customer scope conflicts with intake fields")
            scoped_fields["customer_id"] = requested_customer
        if str(environment_id or "").strip():
            requested_environment = str(environment_id).strip()
            existing_environment = str(scoped_fields.get("environment_id", "") or "").strip()
            if existing_environment and existing_environment != requested_environment:
                raise OrderTransitionError("environment scope conflicts with intake fields")
            scoped_fields["environment_id"] = requested_environment
        # Owner ruling : bounded child fan-out (8 total / 2 live).
        # An idempotent re-entry with the same invocation key is not a new child.
        parent_contract = scoped_fields.get("_workflow_parent")
        if (str(invocation_key or "").strip() and isinstance(parent_contract, dict)
                and parent_contract.get("parent_order_id")):
            existing = self.business.order_by_invocation_key(
                invocation_key, project_id=project_id)
            if existing is None:
                self.business.assert_child_order_capacity(
                    project_id, str(parent_contract["parent_order_id"]))
        return self.business.create_order(
            workflow_id=package.id,
            project_id=project_id,
            org_id=package.org_id,
            fields=scoped_fields,
            source=source,
            source_task_id=source_task_id,
            invocation_key=invocation_key,
            status=status,
            actor=actor,
            acceptance_criteria=[
                str(item)
                for item in list(package.manifest.get("acceptance", []) or [])
                if str(item).strip()
            ],
            workflow_version=str(package.manifest.get("version", "") or ""),
        )

    # ── contractual acceptance ──
    # The owner's round -- evidence by content, the frozen round, the decision, 确认收货并结案, 返工 -- is
    # acceptance_round's (C5-2); the console's handlers go there. What stays here is the payload the order list shows
    # and, under their old names, the store's raw record writes: the launch pin's define_acceptance and the tests that
    # set a record up. They check nothing a round needs (no frozen files); nothing owner-facing calls them.
    def acceptance(self, order_id: str, *, project_id: str | None = None) -> dict[str, Any] | None:
        return self._acceptance_payload(order_id, project_id=project_id)

    def define_acceptance(self, order_id: str, **kwargs: Any) -> AcceptanceRecord:
        return self.business.define_acceptance(order_id, **kwargs)

    def add_acceptance_evidence(self, order_id: str, **kwargs: Any) -> AcceptanceRecord:
        return self.business.add_acceptance_evidence(order_id, **kwargs)

    def submit_acceptance(self, order_id: str, **kwargs: Any) -> AcceptanceRecord:
        return self.business.submit_acceptance(order_id, **kwargs)

    def decide_acceptance(self, order_id: str, **kwargs: Any) -> AcceptanceRecord:
        return self.business.decide_acceptance(order_id, **kwargs)

    def start_acceptance_rework(self, order_id: str, **kwargs: Any) -> AcceptanceRecord:
        return self.business.start_acceptance_rework(order_id, **kwargs)

    async def dispatch_acceptance_rework(
        self,
        order_id: str,
        *,
        request_id: str,
        project_id: str,
    ) -> BusinessOutboxRecord:
        """Deliver one durable contractual rework command to the runtime."""

        if self._acceptance_rework is None:
            raise OrderTransitionError(
                "acceptance rework runtime dispatcher is unavailable"
            )
        async with self._acceptance_rework_dispatch_lock:
            command = self.business.pending_acceptance_rework_command(
                order_id,
                request_id=request_id,
                project_id=project_id,
            )
            if command is None:
                raise OrderTransitionError(
                    "acceptance rework runtime command is not pending"
                )
            payload = dict(command.payload or {})
            try:
                runtime = await self._acceptance_rework(
                    project_id=str(
                        payload.get("project_id") or project_id or "default"
                    ).strip()
                    or "default",
                    launched_task_id=str(
                        payload.get("launched_task_id", "") or ""
                    ).strip(),
                    target_node=str(
                        payload.get("target_node", "") or ""
                    ).strip(),
                    feedback=str(payload.get("feedback", "") or "").strip(),
                    request_id=str(
                        payload.get("request_id", "") or ""
                    ).strip(),
                    command_id=command.outbox_id,
                )
            except Exception as exc:
                self.business.record_acceptance_rework_failure(
                    command.outbox_id, str(exc)
                )
                raise
            return self.business.complete_acceptance_rework(
                command.outbox_id,
                runtime_run_id=str(runtime.get("run_id", "") or "").strip(),
                runtime_session_id=str(
                    runtime.get("runtime_session_id", "") or ""
                ).strip(),
                runtime_task_id=str(
                    runtime.get("runtime_task_id", "") or ""
                ).strip(),
                project_id=project_id,
            )

    async def recover_pending_acceptance_reworks(
        self, *, limit: int = 100
    ) -> list[BusinessOutboxRecord]:
        """Replay commands committed before a process interruption."""

        recovered: list[BusinessOutboxRecord] = []
        for command in self.business.list_pending_acceptance_rework_commands(
            limit=limit
        ):
            project_id = str(
                command.payload.get("project_id", "") or "default"
            ).strip() or "default"
            request_id = str(
                command.payload.get("request_id", "") or ""
            ).strip()
            try:
                recovered.append(
                    await self.dispatch_acceptance_rework(
                        command.aggregate_id,
                        request_id=request_id,
                        project_id=project_id,
                    )
                )
            except Exception:
                # It remains pending.  Recovery is bounded and another broken
                # order must not suppress healthy commands behind it.
                continue
        return recovered

    async def review_order(
        self,
        order_id: str,
        *,
        decision: str,
        actor: str,
        actor_employee_id: str = "",
        actor_role: str = OWNER,
        note: str = "",
        project_id: str = "default",
        _stop_source_observer: Any = None,
        _source_runtime_store: Any = None,
        _employee_trial: Any = None,
    ) -> WorkOrder:
        """Approve (launch the production workflow) or reject a pending order.

        An employee may review only an order whose reviewer they are; the
        owner may review any order and is recorded as the reviewer.
        """
        if _source_runtime_store is not None and _stop_source_observer is None:
            raise OrderTransitionError("fresh_launch_source_schema_unavailable") from None
        clean = str(decision or "").strip().lower()
        is_rejection = clean in {"reject", "deny", "打回", "驳回"}
        if not is_rejection and clean not in {"approve", "launch", "放行", "通过"}:
            raise OrderTransitionError(f"unknown review decision {decision!r}")
        async with self._launch_dispatch_lock:
            if _employee_trial is not None:
                from .employee_workflow_trial import _EmployeeWorkflowTrialLaunch
                if type(_employee_trial) is not _EmployeeWorkflowTrialLaunch:
                    raise OrderTransitionError("employee_trial_contract_invalid")
                # Waiting for this shared lock never retains an old WS grant.
                _employee_trial.check_authority()
            order = self.business.get_order(order_id, project_id=project_id)
            if order is None:
                raise OrderTransitionError(f"order {order_id!r} not found")
            ConsoleAPI._require_order_reviewer(order, actor_employee_id=actor_employee_id, actor_role=actor_role)
            reviewer = actor_employee_id or order.reviewer_employee_id or ""
            if is_rejection:
                if order.status not in {"pending_review", "launching"}:
                    raise OrderTransitionError(
                        f"order {order_id} is {order.status}, not pending_review"
                    )
                # A rejection is an operational hand-off, not a boolean
                # toggle.  Require a durable explanation while the order is
                # still awaiting review so the next attempt has actionable
                # feedback.  Keep the launching branch permissive: an
                # in-flight approval may race with a reject request and must
                # resolve through the existing launch lock/state conflict.
                if order.status == "pending_review" and not str(note or "").strip():
                    raise OrderTransitionError("rejection feedback required")
                return self.business.transition(
                    order_id,
                    "rejected",
                    actor=actor,
                    note=note,
                    reviewer_employee_id=reviewer,
                    project_id=project_id,
                )
            if getattr(order, "source", "") == "canvas_employee_trial":
                from .employee_workflow_trial import _EmployeeWorkflowTrialLaunch
                if type(_employee_trial) is not _EmployeeWorkflowTrialLaunch:
                    raise OrderTransitionError("employee_trial_readback_required")
                _employee_trial.check_order(order, own_creation=True)
                if actor != _employee_trial.actor or project_id != _employee_trial.project_id:
                    raise OrderTransitionError("employee_trial_scope_denied")
            elif _employee_trial is not None:
                raise OrderTransitionError("employee_trial_contract_mismatch")
            if order.status == "launched" and order.launched_task_id:
                return order
            if order.status not in {"pending_review", "launching"}:
                raise OrderTransitionError(
                    f"order {order_id} is {order.status}, not pending_review"
                )
            package = self.package(order.workflow_id)
            if package is None:
                raise OrderTransitionError(
                    f"workflow {order.workflow_id!r} is no longer available"
                )
            # An order left on another project's own flow (placed before create_order refused it) is refused here,
            # before the claim: a launch record written first would stop at the run's preparation, leave the order
            # 「发起中」 and be retried by every recovery. The order stays as it was; rejecting it is still open.
            self._refuse_other_projects_flow(package, project_id, doing="发起")
            if _employee_trial is not None:
                # Recheck inside the original launch lock, immediately before
                # the original claim. A preview is neither a pin nor a grant.
                _employee_trial.check_package(self, package)
                if (order.status != "pending_review" or
                        self.business.pending_launch_command(order_id, project_id=project_id) is not None):
                    return order
            # A saved employee binding is not runtime assembly. Refuse before
            # looking up/claiming a command, building a plan or granting money.
            employee_nodes = employee_binding_runtime_findings(package.nodes)
            if employee_nodes and self.employee_runtime_host is None:
                assert_employee_binding_runtime_available(package.nodes)
            if _stop_source_observer is not None and not employee_nodes:
                # Private synchronous source notification, never permission.
                # Only a true fresh CAS producer may select this path. A clean
                # historical pending command must never be upgraded to fresh.
                from types import FunctionType
                from .fresh_order_launch import _FRESH_LAUNCH_DELIVERY, _FreshOrderLaunchClaimResult

                from ...database.source_project_scope import validate_source_project
                try:
                    validate_source_project(project_id)
                except Exception:
                    raise OrderTransitionError("fresh_launch_project_unavailable") from None
                if (type(_stop_source_observer) is not FunctionType
                        or _stop_source_observer.__code__.co_flags & (0x20 | 0x80 | 0x200)):
                    raise OrderTransitionError("fresh_launch_source_observer_invalid") from None
                source_schema_unavailable = False
                try:
                    from ...database.store import AmpulyStore as _SourceStore
                    source_business = self.business
                    if type(_source_runtime_store) is not _SourceStore or type(source_business) is not BusinessStore:
                        raise RuntimeError("unavailable")
                    source_db = _SourceStore._require_db(_source_runtime_store)
                    source_runtime_path = _source_runtime_store.db_path
                    source_business_path = source_business.path
                    if _SourceStore._verify_fresh_launch_source_schema(_source_runtime_store) is not None:
                        raise RuntimeError("unavailable")
                    source_native = source_db._conn
                    if BusinessStore._verify_fresh_launch_source_schema(source_business) is not None:
                        raise RuntimeError("unavailable")
                    if (self.business is not source_business
                            or source_business.path is not source_business_path
                            or _SourceStore._require_db(_source_runtime_store) is not source_db
                            or type(_source_runtime_store.db_path) is not str
                            or _source_runtime_store.db_path != source_runtime_path
                            or type(_source_runtime_store.project_id) is not str
                            or _source_runtime_store.project_id != project_id
                            or _SourceStore.is_ready.fget(_source_runtime_store) is not True
                            or source_db._conn is not source_native
                            or source_native.in_transaction is not False):
                        raise RuntimeError("unavailable")
                except Exception:
                    source_schema_unavailable = True
                if source_schema_unavailable:
                    raise OrderTransitionError("fresh_launch_source_schema_unavailable") from None
                if self.business.pending_launch_command(order_id, project_id=project_id) is not None:
                    raise OrderTransitionError("fresh_launch_existing_command_conflict") from None
                self._refuse_batch_without_authorization(order, package, project_id)
                fresh = self.business.claim_for_launch(
                    order_id, actor=actor, project_id=project_id,
                    reviewer_employee_id=reviewer, note=note,
                    workflow_version=str(package.manifest.get("version", "") or ""),
                    model_plan=self._build_model_plan(), _fresh_delivery=_FRESH_LAUNCH_DELIVERY,
                )
                if type(fresh) is not _FreshOrderLaunchClaimResult:
                    raise OrderTransitionError("fresh_launch_claim_conflict") from None
                return await self._dispatch_fresh_launch_claim(
                    fresh, package, _stop_source_observer=_stop_source_observer,
                )
            command = self.business.pending_launch_command(order_id, project_id=project_id)
            if command is None:
                # 4.7-A08: an order that will send a list out in batch starts only under an approved, unexpired
                # distribution authorization that names every row of its list. Checked before the claim, so a
                # refused order stays 「待审单」 and nothing was launched.
                self._refuse_batch_without_authorization(order, package, project_id)
                # Built at the claim, and not again: this is the moment the order is placed, and the settings
                # in force at this moment are the ones it runs under from now on.
                if employee_nodes:
                    self.business.ensure_order_allowance(order_id, project_id=project_id, actor=actor)
                model_plan = self._build_model_plan()
                employee_pins = (self.employee_runtime_host.freeze_for_launch(project_id, package, order_id=order_id)
                    if employee_nodes else None)
                if _employee_trial is not None:
                    # Readiness/pin resolution may do I/O. Recheck after it,
                    # with no await before the original durable claim.
                    _employee_trial.check_authority()
                command = self.business.claim_for_launch(
                    order_id,
                    actor=actor,
                    project_id=project_id,
                    reviewer_employee_id=reviewer,
                    note=note,
                    workflow_version=str(package.manifest.get("version", "") or ""),
                    model_plan=model_plan,
                    employee_pins=employee_pins,
                )
            if command is None:
                raise OrderTransitionError(
                    f"order {order_id} is already being launched"
                )
            if employee_nodes and _stop_source_observer is not None:
                # Distinct original employee source, not forged legacy/fresh
                # delivery. Keep the current actor observation for this path.
                source = self.business.read_employee_order_claim_snapshot(project_id=project_id,
                    order_id=order_id, outbox_id=command.outbox_id, workflow_id=package.id,
                    expected_payload=command.payload, include_launch_source=True)
                _stop_source_observer(source)
            if _employee_trial is not None:
                _employee_trial.check_authority()
            return await self._dispatch_launch_command(command, package)

    async def _dispatch_fresh_launch_claim(
        self, fresh: Any, package: WorkflowPackage, *, _stop_source_observer: Any,
    ) -> WorkOrder:
        """One confirmed fresh inflight generation, no historical recovery.

        None callers retain _dispatch_launch_command verbatim. An observer is
        synchronous internal retention only: no public Stop ref, send await or
        returned bool grants authority. This does not close the final Stop gate.
        Every failure after confirmed reservation keeps it off pending recovery.
        """
        from types import FunctionType
        from hashlib import sha256
        from .fresh_order_launch import _FreshOrderLaunchClaimResult
        from .order_launch_claim_source import OriginalOrderLaunchClaim, _legacy_payload
        from .employee_order_launch_anchor import _ReadBudget, _json_dict

        if (type(fresh) is not _FreshOrderLaunchClaimResult
                or type(fresh.source) is not OriginalOrderLaunchClaim
                or type(_stop_source_observer) is not FunctionType
                or _stop_source_observer.__code__.co_flags & (0x20 | 0x80 | 0x200)
                or type(self.business) is not BusinessStore):
            raise OrderTransitionError("fresh_launch_source_invalid") from None
        source = fresh.source
        business = self.business
        business_path = business.path
        selectors = None
        confirmed_inflight = False
        # Source has already been committed/closed by the fresh producer. The
        # request identifies a generation, not a current actor or execution grant.
        primary = None
        result = None
        try:
            source_text_names = ("project_id", "order_id", "outbox_id", "workflow_id", "workflow_version",
                "launch_payload_sha256", "claim_event_id", "event_chain_head_sha256")
            if any(type(getattr(source, name)) is not str for name in source_text_names):
                raise OrderTransitionError("fresh_launch_source_invalid")
            if (type(source.launch_payload_bytes) is not bytes
                    or not 0 < len(source.launch_payload_bytes) <= 131072
                    or type(source.launch_payload_sha256) is not str
                    or sha256(source.launch_payload_bytes).hexdigest() != source.launch_payload_sha256
                    or type(fresh.sequence) is not int or not 0 < fresh.sequence <= 2**63 - 1
                    or type(fresh.created_at) is not str or len(fresh.created_at) > 128):
                raise OrderTransitionError("fresh_launch_source_invalid")
            command = BusinessStore.read_launch_delivery_fixed(business,
                source.outbox_id, order_id=source.order_id, project_id=source.project_id,
                expected_payload_sha256=source.launch_payload_sha256)
            if (command is None or command.status != "inflight"
                    or command.sequence != fresh.sequence or command.created_at != fresh.created_at
                    or command.attempt_count != 0 or command.last_error or command.published_at):
                raise OrderTransitionError("fresh_launch_generation_conflict")
            selectors = (source.outbox_id, source.order_id, source.project_id, source.launch_payload_sha256)
            confirmed_inflight = True
            source_fields = object.__getattribute__(source, "__dict__")
            source_names = ("project_id", "order_id", "outbox_id", "workflow_id", "workflow_version",
                "launch_payload_bytes", "launch_payload_sha256", "claim_event_id", "event_chain_head_sha256")
            if (type(source_fields) is not dict or len(source_fields) != 9
                    or any(type(key) is not str for key in source_fields) or set(source_fields) != set(source_names)):
                raise OrderTransitionError("fresh_launch_source_invalid")
            source_values = tuple(source_fields[name] for name in source_names)
            def source_unchanged():
                if (self.business is not business or type(business) is not BusinessStore or business.path is not business_path
                        or type(source) is not OriginalOrderLaunchClaim
                        or object.__getattribute__(source, "__dict__") is not source_fields
                        or len(source_fields) != 9 or any(type(key) is not str for key in source_fields)
                        or set(source_fields) != set(source_names)):
                    raise OrderTransitionError("fresh_launch_source_changed")
                current = tuple(source_fields[name] for name in source_names)
                if any(type(left) is not type(right) for left, right in zip(current, source_values)) or current != source_values:
                    raise OrderTransitionError("fresh_launch_source_changed")
            budget = _ReadBudget()
            budget.charge_bytes(len(source.launch_payload_bytes))
            payload = _json_dict(source.launch_payload_bytes.decode("utf-8"), budget)
            version, fields, plan = _legacy_payload(payload, project_id=source.project_id,
                order_id=source.order_id, workflow_id=source.workflow_id)
            if (version != source.workflow_version or package.id != source.workflow_id
                    or version and str(package.manifest.get("version", "") or "").strip() != version):
                raise OrderTransitionError("fresh_launch_generation_conflict")
            assert_employee_binding_runtime_available(package.nodes)
            notification = _stop_source_observer(source)
            if notification is not None:
                raise OrderTransitionError("fresh_launch_source_observer_invalid")
            source_unchanged()
            if not fields.get("_workflow_parent"):
                business.ensure_order_allowance(source.order_id, project_id=source.project_id, actor="launch")
            receipt = await self._launch(source.project_id, package, fields,
                order_id=source.order_id, command_id=source.outbox_id,
                model_plan=plan, launch_source=source, record_creation_origin=True)
            source_unchanged()
            result = BusinessStore.complete_launch_delivery_fixed(business,
                source=source, task_id=receipt.task_id, org_id=receipt.org_id)
        except BaseException as exc:
            primary = exc
        if primary is not None:
            # Exact confirmed inflight only; the original native method keeps
            # status unknown and does not reset attempt_count or resend. If
            # publication/cancellation already won, quarantine may refuse; that
            # secondary failure never changes the primary outcome into a retry.
            if confirmed_inflight and type(business) is BusinessStore and business.path is business_path:
                try:
                    BusinessStore.mark_launch_delivery_outcome_unknown(business,
                        selectors[0], order_id=selectors[1], project_id=selectors[2],
                        expected_payload_sha256=selectors[3])
                except BaseException:
                    pass
            if isinstance(primary, Exception):
                # Outside its handler, no original private message/cause/context.
                raise OrderTransitionError("launch_delivery_outcome_unknown") from None
            # Includes original cancellation/SystemExit and B72 source halt.
            raise primary
        return result

    @staticmethod
    def _require_order_reviewer(order: WorkOrder, *, actor_employee_id: str, actor_role: str) -> None:
        """Original review condition, shared without minting a permission."""
        if actor_role != OWNER:
            # An order without an assigned reviewer is the owner's to review;
            # an employee may only sign the orders that name them.
            if (
                not actor_employee_id
                or not order.reviewer_employee_id
                or order.reviewer_employee_id != actor_employee_id
            ):
                raise OrderTransitionError(
                    "only the assigned reviewer or the owner may review this order"
                )

    def _refuse_batch_without_authorization(self, order: WorkOrder, package: Any, project_id: str) -> None:
        """4.7-A08 批量分发前检查: a flow with a batch step sends the order's list out row by row, so the order is
        launched only under an approved distribution authorization (design 3.2, 「每单一张」). Any other flow passes
        untouched. The refusal is the owner's sentence, with what to do next.

        The rows are named the way the kernel names them (read_batch_item_keys, LP2-A); the same check runs again on
        every read of the list while the step works it (browser_connector.bind_order_inputs), so a revoked or
        expired authorization stops the step at its next row, not only at launch.

        D1 (policy): every batch step must have said which of 上传 / 发布 / 回查 it does -- one that
        has not is refused first, in the owner's own sentence -- and the authorization must name every site of every
        batch step among its channels and every step's action among its actions."""
        from ampuly.plugins.office_ui.business_store import DISTRIBUTION_ACTION_MISSING_ZH, DISTRIBUTION_ACTIONS

        lists = batch_lists_of(package)
        if not lists:
            return
        steps = batch_steps_of(package)
        undeclared = [step for step in steps if step["action"] not in DISTRIBUTION_ACTIONS]
        if undeclared:
            name = undeclared[0]["name"]
            raise OrderTransitionError(
                f"{f'「{name}」' if name else ''}{DISTRIBUTION_ACTION_MISSING_ZH}工单已经建好、还没有发起。"
            )
        targets, problem = read_batch_item_keys(order, lists)
        if targets is None:
            # A list that cannot be read cannot be checked against an authorization; passing it on "any approved
            # authorization" would send rows nobody authorised. The batch step refuses such a list too -- and a list
            # where two rows are one once cut would have one authorization line cover both.
            raise OrderTransitionError(
                f"这张工单要把清单批量发出去，但{problem['why']}，没法核对分发授权单。"
                f"工单已经建好、还没有发起；{problem['fix']}后，再回工单表点「放行并发起」。"
            )
        gate = self.business.distribution_authorization_gate(
            order.order_id, project_id=project_id, targets=targets, steps=steps,
        )
        if not gate.get("ok"):
            raise OrderTransitionError(
                f"这张工单要把清单批量发出去，发起前要有一张批准了、还在有效期内、覆盖批量步骤的网站和动作的分发授权单："
                f"{gate.get('why_zh', '')}"
                "工单已经建好、还没有发起；在工单详情的「分发授权单」里起草并批准后，再回工单表点「放行并发起」。"
            )

    def _build_model_plan(self, *, source: str = "global") -> dict[str, Any]:
        """The model plan this order will be frozen under, or ``{}`` when nothing supplies the settings.

        ``{}`` rather than a guess: a plan built from a config this layer invented would be a record of nothing.
        The refusal for a run that needs one and has none is E7's, where the fact is actually known.
        """
        if not callable(self._model_plan_source):
            return {}
        try:
            from ampuly.plugins.office_ui.order_model_plan import build_plan

            return build_plan(self._model_plan_source(), source=source)
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("could not build the order's model plan from the live settings")
            return {}

    def _plan_for_dispatch(self, order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """The plan this launch runs under: the one frozen at the claim, or a fresh one for an older row.

        An order placed before plans existed has no plan on its outbox row. Refusing it would be a stop the
        owner did not ask for, so it is given one from today's settings -- **once**, and recorded as such, so
        the trail never claims it was planned at launch.
        """
        found = dict(payload.get("model_plan") or {})
        if found:
            return found
        granted = self._build_model_plan(source="global_on_resume")
        if granted:
            self.business.record_model_plan(order_id, granted, actor="resume")
        return granted

    async def _dispatch_launch_command(
        self,
        command: BusinessOutboxRecord,
        package: WorkflowPackage,
    ) -> WorkOrder:
        payload = dict(command.payload or {})
        try:
            source_import_failed = False
            try:
                from .order_launch_claim_source import OriginalOrderLaunchClaim
            except Exception:
                source_import_failed = True
            if source_import_failed:
                raise OrderTransitionError("employee launch source unavailable") from None
            # The decoded command's payload dict is mutable. Compare it with
            # original pending bytes before model/allowance/launch effects, so
            # removing pins cannot turn an employee command into legacy work.
            launch_source = self.business.read_employee_order_claim_snapshot(
                project_id=payload.get("project_id", "default"),
                order_id=command.aggregate_id, outbox_id=command.outbox_id,
                workflow_id=package.id, expected_payload=payload,
                include_launch_source=True,
            )
            from .employee_launch_source import EmployeeOrderLaunchSource
            employee_launch = type(launch_source) is EmployeeOrderLaunchSource
            if employee_launch:
                if self.employee_runtime_host is None:
                    raise OrderTransitionError("employee runtime is unavailable; original pins retained")
                self.employee_runtime_host.validate_launch(payload["project_id"], package, launch_source)
            elif type(launch_source) is not OriginalOrderLaunchClaim:
                raise OrderTransitionError("original launch source is unavailable")
            expected_version = str(payload.get("workflow_version", "") or "").strip()
            package_version = str(package.manifest.get("version", "") or "").strip()
            if expected_version and package_version and expected_version != package_version:
                raise OrderTransitionError(
                    f"workflow {package.id!r} version drift: order pinned {expected_version}, available {package_version}"
                )
            # A persisted employee snapshot must never be dropped into legacy
            # role execution. Refuse before allowance, model fallback or launch;
            # the complete fixed-baseline/runtime adapter is not connected yet.
            if not employee_launch and (("employee_pins" in payload and
                    (type(payload["employee_pins"]) is not dict or payload["employee_pins"])) or (
                    "employee_pins_fingerprint" in payload and
                    (type(payload["employee_pins_fingerprint"]) is not str or payload["employee_pins_fingerprint"] != ""))):
                raise OrderTransitionError("employee-bound launch is not connected; original pins retained")
            # Legacy commands may have empty/absent pins even though the
            # package now declares a binding. Never repair an allowance first.
            if not employee_launch:
                assert_employee_binding_runtime_available(package.nodes)
            fields = dict(payload.get("fields", {}) or {})
            if not fields.get("_workflow_parent"):
                # Every launched order is held (model-routing phase 0, C12): console, reviewer-approved, recovered and
                # re-entered launches all pass here, under the dispatch lock. An order given ceilings at launch keeps
                # them; any other gets an allowance without one, so each paid call is journaled and an unknown outcome
                # holds the order. A child order spends from the order above it (its run is held at launch instead).
                self.business.ensure_order_allowance(
                    command.aggregate_id, project_id=str(payload.get("project_id") or "default").strip() or "default", actor="launch",
                )
            receipt = await self._launch(
                str(payload.get("project_id") or "default").strip() or "default",
                package,
                fields,
                order_id=command.aggregate_id,
                command_id=command.outbox_id,
                model_plan=(dict(payload["model_plan"]) if employee_launch else
                    self._plan_for_dispatch(command.aggregate_id, payload)),
                launch_source=launch_source,
            )
        except Exception as exc:
            self.business.record_launch_failure(command.outbox_id, str(exc))
            raise
        return self.business.complete_launch(
            command.outbox_id,
            task_id=receipt.task_id,
            org_id=receipt.org_id,
            project_id=str(payload.get("project_id") or "default").strip() or "default",
        )

    async def recover_pending_launches(self, *, limit: int = 100) -> list[WorkOrder]:
        """Deliver durable launch commands left pending by an interrupted process."""

        recovered: list[WorkOrder] = []
        async with self._launch_dispatch_lock:
            decode_diagnostics = []
            bounded_limit = max(1, min(int(limit or 100), 1000))
            commands = self.business.list_pending_launch_commands(
                limit=bounded_limit, decode_diagnostics=decode_diagnostics,
            )
            for code in ("outbox_launch_decode_invalid", "outbox_row_decode_invalid"):
                count = sum(item.code == code for item in decode_diagnostics)
                if count:
                    logger.warning(
                        "business launch recovery: code={} count={} batch_window_limit={}; unreadable rows retained",
                        code, count, bounded_limit,
                    )
            for command in commands:
                if "_employee_workflow_trial" in dict(command.payload.get("fields") or {}):
                    # An uncertain test-order dispatch requires readback and
                    # explicit resolution, never automatic outbox replay.
                    continue
                workflow_id = str(command.payload.get("workflow_id") or "").strip()
                package = self.package(workflow_id)
                if package is None:
                    self.business.record_launch_failure(
                        command.outbox_id,
                        f"workflow {workflow_id!r} is no longer available",
                    )
                    continue
                try:
                    recovered.append(
                        await self._dispatch_launch_command(command, package)
                    )
                except Exception:
                    # The durable command remains pending for a later bounded
                    # recovery pass; one bad order must not block the rest.
                    continue
        return recovered

    async def place_and_launch(
        self,
        *,
        workflow_id: str,
        fields: dict[str, Any],
        actor: str,
        project_id: str = "default",
        customer_id: str = "",
        environment_id: str = "",
        source: str = "console",
        source_task_id: str = "",
        invocation_key: str = "",
        budget_limits: dict[str, Any] | None = None,
        _employee_trial: Any = None,
    ) -> WorkOrder:
        """The owner's one-click path: create the order and approve it at once.

        Every launch goes through an order, so the console's test launches
        and the intake flow's node-triggered launches leave the same trail.
        """
        if _employee_trial is not None:
            from .employee_workflow_trial import _EmployeeWorkflowTrialLaunch
            if type(_employee_trial) is not _EmployeeWorkflowTrialLaunch:
                raise OrderTransitionError("employee_trial_contract_invalid")
            _employee_trial.check_call(project_id=project_id, workflow_id=workflow_id,
                actor=actor, source=source, source_task_id=source_task_id, invocation_key=invocation_key,
                fields=fields, customer_id=customer_id, environment_id=environment_id, budget_limits=budget_limits)
            _employee_trial.check_authority()
            existing_trial = self.business.order_by_invocation_key(invocation_key, project_id=project_id)
            if existing_trial is not None:
                _employee_trial.check_order(existing_trial)
                return existing_trial
        elif source == "canvas_employee_trial" or "_employee_workflow_trial" in fields:
            raise OrderTransitionError("employee_trial_contract_required")
        package = self.package(workflow_id)
        if package is None:
            raise OrderTransitionError(f"workflow {workflow_id!r} not found")
        # Refused before prepare_invocation below, which installs the flow and writes its tool grants into this
        # project's allowlist: another project's flow must not get that far (create_order checks again).
        self._refuse_other_projects_flow(package, project_id)
        if _employee_trial is not None:
            _employee_trial.check_package(self, package)
        # A durable node invocation that already exists is a recovery, not a
        # fresh deployment.  Re-run create_order only for its exact-contract
        # collision check, then resume the existing launch state without
        # reinstalling a possibly newer package first.
        if str(invocation_key or "").strip() and self.business.order_by_invocation_key(
            invocation_key, project_id=project_id
        ) is not None:
            order = self.create_order(
                workflow_id=workflow_id,
                fields=fields,
                project_id=project_id,
                customer_id=customer_id,
                environment_id=environment_id,
                source=source,
                source_task_id=source_task_id,
                invocation_key=invocation_key,
                actor=actor,
            )
            if employee_binding_runtime_findings(package.nodes):
                if order.status not in {"pending_review", "launching"}:
                    # Keep the original launched receipt/status refusal, but
                    # do not repair a budget for an unavailable bound run.
                    return await self.review_order(
                        order.order_id,
                        decision="approve",
                        actor=actor,
                        actor_role=OWNER,
                        project_id=project_id,
                    )
                if self.employee_runtime_host is None:
                    assert_employee_binding_runtime_available(package.nodes)
            if budget_limits is not None and self.business.open_budget_for_order(order.order_id, project_id=project_id) is None:
                # The first attempt stopped between the order and its allowance: grant it now, once.
                self.business.create_run_budget(
                    project_id=project_id, customer_id=order.customer_id, environment_id=order.environment_id,
                    order_id=order.order_id, limits=self.business.clean_order_allowance(budget_limits), actor=actor,
                )
            return await self.review_order(
                order.order_id,
                decision="approve",
                actor=actor,
                actor_role=OWNER,
                project_id=project_id,
            )
        # Validate ceilings before prepare_invocation can install an org/skill
        # or write tool grants, not merely before the order record is created.
        clean_limits = self.business.clean_order_allowance(budget_limits) if budget_limits is not None else None
        # Validate the intake before creating a durable record.
        needs_runtime_install = not bool(employee_binding_runtime_findings(package.nodes))
        if not needs_runtime_install:
            if self.employee_runtime_host is None:
                assert_employee_binding_runtime_available(package.nodes)
            # Validate only; freezing and installation happen after the
            # actual order/reviewer claim, not as an implicit trial side effect.
            from .workflow_packages import compose_intake
            compose_intake(package, fields)
        else:
            validate_invocation(self.ampuly_home, project_id, package, fields)
        if _employee_trial is not None:
            _employee_trial.check_authority()
        order = self.create_order(
            workflow_id=workflow_id,
            fields=fields,
            project_id=project_id,
            customer_id=customer_id,
            environment_id=environment_id,
            source=source,
            source_task_id=source_task_id,
            invocation_key=invocation_key,
            actor=actor,
        )
        if _employee_trial is not None:
            # The existing invocation UNIQUE + exact-fields transaction makes
            # a losing simultaneous creator fail, even across API instances.
            _employee_trial.check_order(order, own_creation=True)
            _employee_trial.check_authority()
        if clean_limits is not None:
            # Granted before the run starts, so the run's first task is pinned to it and every work item inherits it.
            self.business.create_run_budget(
                project_id=project_id, customer_id=order.customer_id, environment_id=order.environment_id,
                order_id=order.order_id, limits=clean_limits, actor=actor,
            )
        # All order scope/adoption and budget-record checks must succeed before
        # the legacy runtime installer can mutate org/skills/staffing/grants.
        # The claimed launch still revalidates; this is not an atomic publisher.
        if needs_runtime_install and self.runtime_publication_host is None:
            prepare_invocation(self.ampuly_home, project_id, package, fields, legacy_mutable_install=True)
        if _employee_trial is not None:
            return await self.review_order(order.order_id, decision="approve", actor=actor,
                actor_role=OWNER, project_id=project_id, _employee_trial=_employee_trial)
        return await self.review_order(order.order_id, decision="approve", actor=actor, actor_role=OWNER, project_id=project_id)

    def mark_order_by_task(self, task_id: str, to_status: str, *, actor: str = "runtime", note: str = "", project_id: str | None = None) -> WorkOrder | None:
        """Advance the order whose launched task is ``task_id`` (producing / delivering / closed)."""
        order = self.business.order_for_launched_task(task_id, project_id=project_id)
        if order is None:
            return None
        try:
            return self.business.transition(order.order_id, to_status, actor=actor, note=note, project_id=project_id)
        except OrderTransitionError:
            return order

    # What the production run's own status says about its order. A run that starts is producing; a run that
    # finishes has something to hand over, so the order is delivering and its acceptance can begin. Only forward:
    # going back from delivering to producing is the contractual rework's move, made with its own record.
    _ORDER_STEPS_FOR_RUN_STATUS = {
        "running": (("launched", "producing"),),
        "done": (("launched", "producing"), ("producing", "delivering")),
    }

    def follow_order_run(self, task_id: str, run_status: str, *, project_id: str | None = None) -> WorkOrder | None:
        """Move the order this run belongs to as far as the run's status proves, and no further.

        Without this nothing ever took an order past ``launched``: the rework of a rejected acceptance is refused
        from there, so a real order could be delivered and never reworked. A failed, cancelled or waiting run says
        nothing about delivery and moves nothing; a task that is not an order's run is ignored.
        """
        steps = self._ORDER_STEPS_FOR_RUN_STATUS.get(str(run_status or "").strip().lower())
        if not steps:
            return None
        order = self.business.order_for_launched_task(task_id, project_id=project_id)
        if order is None:
            return None
        for from_status, to_status in steps:
            if order.status != from_status:
                continue
            try:
                order = self.business.transition(
                    order.order_id, to_status, actor="runtime", note=f"production run {run_status}", project_id=order.project_id,
                )
            except OrderTransitionError:
                return order
        return order
