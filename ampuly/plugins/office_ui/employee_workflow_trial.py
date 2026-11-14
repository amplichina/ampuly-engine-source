"""Saved employee workflows tested through the original business-order launch.

This server leaf is not a WS handler, another runner, or a grant of permissions.
Its labels and nonce identify a request only. Runtime pins, budgets, approvals,
Task/Run provenance and delivery remain the original launch path's responsibility.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import inspect
import json
import re
import uuid
from typing import Any, Callable

import yaml

from .ai_employee_actions import AuthenticatedActor, AuthorizedProject
from .ai_employee_contract import AIEmployeeContractError
from .business_store import OrderTransitionError, WorkOrder
from .workflow_packages import INVOKABLE, compose_intake
from .workflow_snapshot_io import read_workflow_tree


SOURCE = "canvas_employee_trial"
_FIELD = "_employee_workflow_trial"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")


def _refuse(code: str) -> None:
    raise OrderTransitionError(code)


def _check_authority(callback: Callable[[], None] | None) -> None:
    """A server-owned synchronous checkpoint, never serialized permission."""
    if callback is None:
        return  # Existing trusted internal callers; WS always supplies one.
    try:
        if not callable(callback) or inspect.iscoroutinefunction(callback):
            _refuse("employee_trial_forbidden")
        result = callback()
        if result is not None:
            if inspect.iscoroutine(result):
                result.close()
            _refuse("employee_trial_forbidden")
    except Exception:
        _refuse("employee_trial_forbidden")


def _json(value: Any) -> str:
    pending, count, text_bytes = [(value, 0)], 0, 0
    try:
        while pending:
            item, depth = pending.pop()
            count += 1
            if count > 4096 or depth > 16:
                _refuse("employee_trial_input_too_large")
            if type(item) is dict:
                if len(item) > 4096 or any(type(key) is not str for key in item):
                    _refuse("employee_trial_input_invalid")
                pending.extend((child, depth + 1) for pair in item.items() for child in pair)
            elif type(item) is list:
                if len(item) > 4096:
                    _refuse("employee_trial_input_too_large")
                pending.extend((child, depth + 1) for child in item)
            elif type(item) is str:
                if len(item) > 65536:
                    _refuse("employee_trial_input_too_large")
                text_bytes += len(item.encode("utf-8"))
                if text_bytes > 65536:
                    _refuse("employee_trial_input_too_large")
            elif item is not None and type(item) not in (int, float, bool):
                _refuse("employee_trial_input_invalid")
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        length = len(result.encode("utf-8"))
    except OrderTransitionError:
        raise
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _refuse("employee_trial_input_invalid")
    if length > 65536:
        _refuse("employee_trial_input_too_large")
    return result


def _scope(actor: AuthenticatedActor, scope: AuthorizedProject) -> None:
    if (type(actor) is not AuthenticatedActor or type(actor.actor) is not str or not actor.actor.strip()
            or type(scope) is not AuthorizedProject or scope.is_owner is not True
            or type(scope.project_id) is not str or not _IDENTIFIER.fullmatch(scope.project_id)):
        _refuse("employee_trial_scope_denied")


def _key(project_id: str, request_id: str) -> str:
    return "employee-workflow-trial:" + hashlib.sha256(_json([project_id, request_id]).encode()).hexdigest()


@dataclass(frozen=True)
class _EmployeeWorkflowTrialLaunch:
    """Server-created immutable request; never deserialize this from a payload."""
    project_id: str
    actor: str
    request_id: str
    workflow_id: str
    workflow_version: str
    manifest_sha256: str
    environment_id: str
    customer_id: str
    input_json: str
    limits_json: str
    request_digest: str
    dispatch_nonce: str
    recheck_authority: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def check_authority(self) -> None:
        _check_authority(self.recheck_authority)

    @property
    def invocation_key(self) -> str:
        return _key(self.project_id, self.request_id)

    @property
    def fields(self) -> dict[str, Any]:
        return {**json.loads(self.input_json), "project_id": self.project_id,
                "customer_id": self.customer_id, "environment_id": self.environment_id,
                _FIELD: {"schema_version": 1, "request_digest": self.request_digest,
                         "workflow_version": self.workflow_version, "manifest_sha256": self.manifest_sha256,
                         "budget_limits": json.loads(self.limits_json), "dispatch_nonce": self.dispatch_nonce}}

    def check_call(self, *, project_id, workflow_id, actor, source, source_task_id,
                   invocation_key, fields, customer_id, environment_id, budget_limits) -> None:
        if (project_id != self.project_id or workflow_id != self.workflow_id or actor != self.actor
                or source != SOURCE or source_task_id != "" or invocation_key != self.invocation_key
                or customer_id != self.customer_id or environment_id != self.environment_id
                or _json(fields) != _json(self.fields) or _json(budget_limits) != self.limits_json):
            _refuse("employee_trial_contract_mismatch")

    def check_order(self, order: WorkOrder, *, own_creation: bool = False) -> None:
        note = order.fields.get(_FIELD)
        if (order.project_id != self.project_id or order.workflow_id != self.workflow_id
                or order.source != SOURCE or order.invocation_key != self.invocation_key
                or type(note) is not dict or note.get("request_digest") != self.request_digest):
            _refuse("employee_trial_request_conflict")
        expected, actual = self.fields, dict(order.fields)
        expected[_FIELD] = {**expected[_FIELD], "dispatch_nonce": note.get("dispatch_nonce")}
        if _json(expected) != _json(actual):
            _refuse("employee_trial_request_conflict")
        if own_creation and note.get("dispatch_nonce") != self.dispatch_nonce:
            _refuse("employee_trial_creation_owned_elsewhere")

    def check_package(self, api, package) -> None:
        if (package is None or package.id != self.workflow_id
                or package.manifest.get("version") != self.workflow_version):
            _refuse("employee_trial_workflow_changed")
        api._refuse_other_projects_flow(package, self.project_id, doing="测试")
        if package.status not in INVOKABLE:
            _refuse("employee_trial_submit_for_testing_required")
        try:
            raw = read_workflow_tree(package.root, max_files=48, max_bytes=512 * 1024)["workflow.yaml"]
            # Same LF canonicalization as WorkflowPackage._file_sha256.
            canonical = raw if b"\0" in raw else raw.replace(b"\r\n", b"\n")
            persisted = yaml.safe_load(raw.decode("utf-8"))
        except (AIEmployeeContractError, OSError, UnicodeError, yaml.YAMLError, KeyError):
            _refuse("employee_trial_saved_workflow_unavailable")
        if (hashlib.sha256(canonical).hexdigest() != self.manifest_sha256
                or _json(persisted) != _json(package.manifest)):
            _refuse("employee_trial_workflow_changed")
        if package.problems:
            _refuse("employee_trial_workflow_invalid")
        if package.manifest.get("tool_grants"):
            from .workflow_packages import existing_tool_grants
            try:
                existing_tool_grants(api.ampuly_home, self.project_id, package)
            except ValueError:
                # Preserve the refusal for missing grants; tests never install them.
                _refuse("employee_trial_automatic_grants_unsupported")
        if not any(type(node) is dict and "employee_binding" in node for node in package.nodes):
            _refuse("employee_trial_bound_workflow_required")
        if api.employee_runtime_host is None:
            _refuse("employee_trial_runtime_unavailable")
        if api.business.project_lifecycle(self.project_id).status != "active":
            _refuse("employee_trial_project_inactive")
        if api.business.project_customer(self.project_id) != self.customer_id:
            _refuse("employee_trial_project_changed")
        declared = {str(field["id"]) for field in package.intake_fields}
        if set(json.loads(self.input_json)) - declared:
            _refuse("employee_trial_unknown_input")
        try:
            compose_intake(package, json.loads(self.input_json))
        except ValueError as exc:
            # Required-field labels help the user correct the saved intake;
            # template/implementation failures remain a fixed public code.
            message = str(exc)
            raise OrderTransitionError(message[:512] if message.startswith("缺少必填项：")
                                       else "employee_trial_intake_invalid") from None
        # Read-only live component/policy/human-assignment checks. Order-time
        # allowance and immutable employee pins are still checked at claim.
        api.employee_runtime_host.validate_for_publication(package)


class EmployeeWorkflowTrialService:
    """Prepare/start/read a full saved flow. Call only with server-resolved scope."""
    def __init__(self, api, *, recheck_authority: Callable[[], None] | None = None):
        self.api = api
        self.recheck_authority = recheck_authority

    def _contract(self, request, actor, scope) -> _EmployeeWorkflowTrialLaunch:
        _scope(actor, scope)
        _check_authority(self.recheck_authority)
        if type(request) is not dict:
            _refuse("employee_trial_request_invalid")
        if request.get("node_id"):
            _refuse("employee_trial_single_node_unsupported")
        if "draft" in request or "nodes" in request:
            _refuse("employee_trial_unsaved_draft_unsupported")
        allowed = {"request_id", "project_id", "workflow_id", "workflow_version", "manifest_sha256",
                   "environment_id", "fields", "budget_limits"}
        if set(request) - allowed or request.get("project_id") != scope.project_id:
            _refuse("employee_trial_request_invalid")
        for key in ("request_id", "workflow_id", "workflow_version", "environment_id"):
            if type(request.get(key)) is not str or not _IDENTIFIER.fullmatch(request[key]):
                _refuse("employee_trial_request_invalid")
        if type(request.get("manifest_sha256")) is not str or not _SHA.fullmatch(request["manifest_sha256"]):
            _refuse("employee_trial_manifest_digest_required")
        fields, budget = request.get("fields"), request.get("budget_limits")
        if (type(fields) is not dict or any(type(k) is not str or k.startswith("_")
                or k in {"project_id", "customer_id", "environment_id"} for k in fields)):
            _refuse("employee_trial_input_invalid")
        if type(budget) is not dict or not budget:
            _refuse("employee_trial_explicit_budget_required")
        _json(fields)
        _json(budget)
        limits = self.api.business.clean_order_allowance(budget)
        # Require at least a finite model-call ceiling; unknown pricing is not
        # zero and a caller may additionally choose the existing CNY contract.
        if "model_call" not in limits:
            _refuse("employee_trial_model_call_limit_required")
        normalized = {key: value if type(value) in (dict, list) else "" if value is None else str(value)
                      for key, value in fields.items()}
        input_json, limits_json = _json(normalized), _json(limits)
        digest = hashlib.sha256(_json({**request, "fields": json.loads(input_json),
            "budget_limits": limits}).encode("utf-8")).hexdigest()
        return _EmployeeWorkflowTrialLaunch(scope.project_id, actor.actor, request["request_id"],
            request["workflow_id"], request["workflow_version"], request["manifest_sha256"],
            request["environment_id"], self.api.business.project_customer(scope.project_id),
            input_json, limits_json, digest, uuid.uuid4().hex, self.recheck_authority)

    def prepare(self, request, *, actor: AuthenticatedActor, scope: AuthorizedProject) -> dict[str, Any]:
        contract = self._contract(request, actor, scope)
        package = self.api.package(contract.workflow_id)
        contract.check_package(self.api, package)
        contract.check_authority()
        return {"state": "ready_for_test_order", "request_digest": contract.request_digest,
                "workflow_id": contract.workflow_id, "workflow_version": contract.workflow_version,
                "manifest_sha256": contract.manifest_sha256, "project_id": contract.project_id,
                "budget_limits": json.loads(contract.limits_json), "creates_business_order": True,
                "human_review": "original_workflow", "execution_permissions": "existing_grants_only",
                "environment_is_permission": False}

    def _view(self, order) -> dict[str, Any]:
        # A launch receipt proves dispatch only, not execution or delivery.
        if order.status == "pending_review":
            state = "creation_recorded_execution_not_confirmed"
        elif order.status == "launching":
            state = "execution_outcome_pending"
        elif order.launched_task_id:
            state = "launch_recorded"
        else:
            state = "order_recorded"
        try:
            budget = self.api.business.open_budget_for_order(order.order_id, project_id=order.project_id)
            budget_readback_state = "available" if budget is not None else "no_open_budget"
        except Exception:
            # Failure to read an allowance does not erase an original order
            # receipt, and is never permission to grant another allowance.
            budget, budget_readback_state = None, "unavailable"
        return {"state": state, "order_id": order.order_id, "project_id": order.project_id,
                "order_status": order.status, "launched_task_id": order.launched_task_id or None,
                "budget": budget, "budget_known": budget is not None, "automatic_retry": False,
                "budget_readback_state": budget_readback_state,
                "execution_succeeded": None, "delivery_confirmed": None,
                "human_review": "original_workflow"}

    def _authorized_view(self, order) -> dict[str, Any]:
        _check_authority(self.recheck_authority)
        result = self._view(order)
        _check_authority(self.recheck_authority)
        return result

    def read(self, *, request_id: str, actor: AuthenticatedActor, scope: AuthorizedProject) -> dict[str, Any]:
        _scope(actor, scope)
        _check_authority(self.recheck_authority)
        if type(request_id) is not str or not _IDENTIFIER.fullmatch(request_id):
            _refuse("employee_trial_request_invalid")
        try:
            order = self.api.business.order_by_invocation_key(_key(scope.project_id, request_id), project_id=scope.project_id)
            if order is None:
                _check_authority(self.recheck_authority)
                return {"state": "not_found", "automatic_retry": False}
            if order.source != SOURCE or type(order.fields.get(_FIELD)) is not dict:
                _refuse("employee_trial_request_conflict")
            return self._authorized_view(order)
        except OrderTransitionError:
            raise
        except Exception:
            _check_authority(self.recheck_authority)
            return {"state": "readback_unavailable", "automatic_retry": False}

    async def start(self, request, *, actor: AuthenticatedActor, scope: AuthorizedProject) -> dict[str, Any]:
        contract = self._contract(request, actor, scope)
        existing = self.api.business.order_by_invocation_key(contract.invocation_key, project_id=scope.project_id)
        if existing is not None:
            contract.check_order(existing)
            return self._authorized_view(existing)
        contract.check_package(self.api, self.api.package(contract.workflow_id))
        contract.check_authority()
        try:
            order = await self.api.place_and_launch(workflow_id=contract.workflow_id, fields=contract.fields,
                actor=actor.actor, project_id=scope.project_id, customer_id=contract.customer_id,
                environment_id=contract.environment_id, source=SOURCE, invocation_key=contract.invocation_key,
                budget_limits=json.loads(contract.limits_json), _employee_trial=contract)
        except Exception:
            # Includes a losing unique-key race and unknown create/launch
            # acknowledgements. Never replenish or dispatch from this branch.
            contract.check_authority()
            try:
                existing = self.api.business.order_by_invocation_key(contract.invocation_key, project_id=scope.project_id)
                if existing is not None:
                    contract.check_order(existing)
                    return self._authorized_view(existing)
            except OrderTransitionError:
                raise
            except Exception:
                pass
            contract.check_authority()
            return {"state": "creation_outcome_pending", "automatic_retry": False,
                    "request_id": contract.request_id, "project_id": scope.project_id}
        contract.check_order(order)
        return self._authorized_view(order)
