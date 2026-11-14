"""Strict owner-only wire bridge for real employee package lifecycle service.

Composition injects an existing EmployeeLifecycle; this module never opens a
store, starts an event loop, reads runtime configuration, or constructs an
executor. Worker observations/authority/candidate files are not wire actions.
The host must give the service its own fresh authorization callback as well.
"""
from __future__ import annotations

from copy import deepcopy
import inspect
import math
import re

from .ai_employee_contract import AIEmployeeContractError, canonical_json

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_TRIAL = re.compile(r"et-[a-f0-9]{24}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_COMMON = {"type", "project_id", "request_id"}
_FIELDS = {
    "employee_package_test_prepare": {"package_id", "expected_revision", "expected_fingerprint"},
    "employee_package_test_start": {"package_id", "expected_revision", "expected_fingerprint",
                                    "reason", "data_scope", "budget"},
    "employee_package_test_detail": {"trial_id"},
    "employee_package_test_lookup": {"original_request_id"},
    "employee_package_review": {"trial_id", "expected_fingerprint", "decision", "judgments", "note"},
    "employee_package_publish": {"package_id", "expected_fingerprint"},
    "employee_package_publish_lookup": {"original_request_id", "package_id", "expected_fingerprint"},
    "employee_package_lifecycle_history": {"package_id"},
    "employee_package_test_abandon": {"trial_id", "expected_state", "reason"},
}
SUPPORTED_ACTIONS = frozenset(_FIELDS)
_METHODS = dict(zip(_FIELDS, ("prepare", "start", "trial_detail", "lookup_trial", "review", "publish", "lookup_publication", "history", "abandon_trial")))
_WRITES = frozenset({"employee_package_test_start", "employee_package_review", "employee_package_publish",
                     "employee_package_test_abandon"})
_OPTIONAL_FIELDS = {"employee_package_test_prepare": {"instance_selection"},
                    "employee_package_test_start": {"instance_selection"}}
LIFECYCLE_FIELDS = {action: frozenset(fields | _OPTIONAL_FIELDS.get(action, set()))
                    for action, fields in _FIELDS.items()}
LIFECYCLE_WRITE = _WRITES
_STATES = {"prepared", "running", "awaiting_review", "approved", "rejected", "failed", "cancelled", "outcome_unknown", "abandoned"}
_ABANDON_KEYS = frozenset({"kind", "trial_id", "package_id", "fingerprint", "prior_state", "actor", "request_id",
                           "reason", "at", "budget_id", "observations_sha256", "spend_reset", "reviewable", "publishable"})
_MESSAGES = {
    "request_invalid": "请求字段无效；未接受客户端运行结果、身份、路径或授权。",
    "owner_required": "当前会话不是这个项目的所有者，或授权已经变化。",
    "unsupported": "岗位测试与发布入口尚未接入。",
    "storage_record_invalid": "服务端测试记录暂不能核实。",
    "outcome_unknown": "操作结果未知；请核对原请求，不要换请求标识重新执行。",
    "revision_conflict": "岗位草稿已变化，请重新查看当前版本。",
    "fingerprint_mismatch": "岗位内容指纹已变化，请重新测试当前版本。",
    "request_id_reused": "同一请求标识不能替换内容或操作人。",
    "evaluation_trial_not_found": "当前项目没有可读取的对应测试。",
    "not_allowed_in_status": "当前状态不允许执行此动作。",
    "publication_test_not_passed": "真实测试与逐项人工质量审核尚未通过。",
    "publication_approval_unavailable": "尚无可核实的发布审核记录。",
    "publication_evaluation_unavailable": "尚无可核实的真实测试记录。",
    "evaluation_runner_unavailable": "当前没有已接通的受控测试执行器。",
    "evaluation_budget_invalid": "测试预算必须有明确金额、调用次数和时长上限。",
    "evaluation_tool_not_authorized": "本轮未授权外部工具能力，不能启动这类测试。",
    "evaluation_pricing_unavailable": "当前模型价格依据不可用，未按零费用继续。",
    "storage_busy": "已有测试或保存正在处理，请先核对原操作。",
    "validation_failed": "岗位合同或测试任务集需要修正。",
}


def _fail(code="request_invalid"):
    raise AIEmployeeContractError(code, _MESSAGES.get(code, _MESSAGES["storage_record_invalid"]))


def _valid(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _exact(value, fields):
    return (type(value) is dict and all(type(key) is str for key in value)
            and set(value) == set(fields))


def _text(value, maximum, *, empty=False):
    return (type(value) is str and len(value) <= maximum and (empty or bool(value.strip()))
            and "\x00" not in value)


def _plain(value, limit):
    # Exact builtin JSON types only, before invoking serializers/equality hooks.
    pending = [(value, 0)]
    seen = set()
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 15000 or depth > 20:
            _fail()
        if type(item) in (dict, list):
            if id(item) in seen:
                _fail()
            seen.add(id(item))
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    _fail()
                pending.extend((child, depth + 1) for child in item.values())
            else:
                pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, bool, int):
            if type(item) is not float or not math.isfinite(item):
                _fail()
    if len(canonical_json(value)) > limit:
        _fail()


def _request(message):
    _plain(message, 64 * 1024)
    if type(message) is not dict or message.get("type") not in SUPPORTED_ACTIONS:
        _fail("unsupported")
    action = message["type"]
    required = _COMMON | _FIELDS[action]
    if not required.issubset(message) or set(message) - required - _OPTIONAL_FIELDS.get(action, set()):
        _fail()
    if "instance_selection" in message:
        from .employee_qualification import validate_selection
        validate_selection(message["instance_selection"])
    if not _valid(message["project_id"], _ID) or not _valid(message["request_id"], _ID):
        _fail()
    for name, pattern in (("package_id", _PACKAGE), ("trial_id", _TRIAL),
                          ("expected_revision", _SHA), ("expected_fingerprint", _SHA),
                          ("original_request_id", _ID)):
        if name in message and not _valid(message[name], pattern):
            _fail()
    if action == "employee_package_test_start":
        budget = message["budget"]
        if (message["data_scope"] != ("instance_taskset" if "instance_selection" in message else "package_taskset") or not _text(message["reason"], 4000)
                or not _exact(budget, {"max_cost_cny", "max_model_calls", "max_wall_clock_s"})
                or type(budget["max_cost_cny"]) is not str
                or re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,6})?", budget["max_cost_cny"]) is None
                or type(budget["max_model_calls"]) is not int or not 1 <= budget["max_model_calls"] <= 128
                or type(budget["max_wall_clock_s"]) is not int or not 1 <= budget["max_wall_clock_s"] <= 600):
            _fail()
    if action == "employee_package_test_abandon" and (
            message["expected_state"] != "outcome_unknown" or not _text(message["reason"], 4000)):
        _fail()
    if action == "employee_package_review":
        judgments = message["judgments"]
        if (message["decision"] not in {"approve", "reject"} or not _text(message["note"], 4000, empty=True)
                or type(judgments) is not list or not 1 <= len(judgments) <= 64):
            _fail()
        identities = set()
        for row in judgments:
            if (not _exact(row, {"case_id", "non_inferior_met", "absolute_threshold_met"})
                    or not _text(row["case_id"], 128) or row["case_id"] in identities
                    or type(row["non_inferior_met"]) is not bool
                    or type(row["absolute_threshold_met"]) is not bool):
                _fail()
            identities.add(row["case_id"])
    return deepcopy(message)


def _context(value):
    expected = {"model_config", "capability_impls", "runtime_profile_fingerprint", "checker_versions",
                "skill_tree_fingerprints", "params_fingerprint"}
    extra = {"qualification_scope", "knowledge_refs_fingerprint"}
    if not (_exact(value, expected) or _exact(value, expected | extra)):
        _fail("storage_record_invalid")
    if extra.issubset(value):
        from .employee_qualification import context_seal_valid
        if not _valid(value["knowledge_refs_fingerprint"], _SHA) or not context_seal_valid(value):
            _fail("storage_record_invalid")
    model = value["model_config"]
    if (not _exact(model, {"provider", "model", "tier"})
            or any(not _text(item, 256) for item in model.values())
            or not _valid(value["runtime_profile_fingerprint"], _SHA)
            or not _valid(value["params_fingerprint"], _SHA)
            or type(value["capability_impls"]) is not dict):
        _fail("storage_record_invalid")
    for field in ("checker_versions", "skill_tree_fingerprints"):
        mapping = value[field]
        if (type(mapping) is not dict or len(mapping) > 64
                or any(not _text(key, 256) or not _text(item, 256) for key, item in mapping.items())):
            _fail("storage_record_invalid")
    # Current installed trial executor authorizes no tools. This is an actual
    # implementation map, not a declaration digest or client capability list.
    if value["capability_impls"] != {}:
        _fail("storage_record_invalid")
    return deepcopy(value)


def _view(raw, action, message):
    if action == "employee_package_lifecycle_history":
        from .employee_lifecycle_history import validate_history_view
        return validate_history_view(raw, project_id=message["project_id"], package_id=message["package_id"])
    if type(raw) is not dict:
        _fail("storage_record_invalid")
    if (not _valid(raw.get("package_id"), _PACKAGE) or not _valid(raw.get("fingerprint"), _SHA)
            or ("package_id" in message and raw["package_id"] != message["package_id"])
            or ("expected_fingerprint" in message and raw["fingerprint"] != message["expected_fingerprint"])):
        _fail("storage_record_invalid")
    result = {"package_id": raw["package_id"], "fingerprint": raw["fingerprint"]}
    allowed = raw.get("allowed_actions")
    if type(allowed) is not list or any(type(item) is not str for item in allowed):
        _fail("storage_record_invalid")
    permitted = SUPPORTED_ACTIONS | {"employee_instance_create"}
    result["allowed_actions"] = list(dict.fromkeys(item for item in allowed if item in permitted))
    revision = raw.get("revision")
    if action in {"employee_package_publish", "employee_package_publish_lookup"}:
        lookup = action == "employee_package_publish_lookup"
        identity = "original_request_id" if lookup else "request_id"
        if (raw.get(identity) != message[identity] or raw.get("result") not in {"success", "outcome_unknown"}
                or raw["result"] == "success" and (raw.get("effects_produced") is not True or not _valid(revision, _SHA))
                or raw["result"] == "success" and allowed != ["employee_instance_create"]
                or raw["result"] == "outcome_unknown" and (raw.get("effects_produced") is not None or allowed != [])):
            _fail("storage_record_invalid")
        if revision is not None and not _valid(revision, _SHA):
            _fail("storage_record_invalid")
        result.update(revision=revision, result=raw["result"], effects_produced=raw["effects_produced"])
        if lookup:
            if (type(raw.get("lookup_found")) is not bool
                    or raw["result"] == "success" and raw["lookup_found"] is not True):
                _fail("storage_record_invalid")
            result.update(original_request_id=raw["original_request_id"], lookup_found=raw["lookup_found"],
                          original_effects_produced=raw["effects_produced"], effects_produced=False)
    else:
        if (raw.get("effects_produced") is not False or not _valid(revision, _SHA)
                or type(raw.get("sample_count")) is not int or not 1 <= raw["sample_count"] <= 64):
            _fail("storage_record_invalid")
        result.update(revision=revision, sample_count=raw["sample_count"], context=_context(raw.get("context")))
        if action == "employee_package_test_prepare":
            expected_scope = "instance_taskset" if "instance_selection" in message else "package_taskset"
            if (revision != message["expected_revision"] or raw.get("data_scope") != expected_scope
                    or raw.get("human_quality_review_required") is not True
                    or allowed != ["employee_package_test_start"]):
                _fail("storage_record_invalid")
            result.update(data_scope=expected_scope, human_quality_review_required=True)
        else:
            observations = raw.get("observations")
            cases = raw.get("cases")
            if (not _valid(raw.get("trial_id"), _TRIAL) or raw.get("state") not in _STATES
                    or ("trial_id" in message and raw["trial_id"] != message["trial_id"])
                    or not _valid(raw.get("candidate_revision"), _SHA)
                    or not _valid(raw.get("test_request_id"), _ID)
                    or action == "employee_package_test_start" and (
                        raw["test_request_id"] != message["request_id"] or raw["candidate_revision"] != message["expected_revision"])
                    or action == "employee_package_test_lookup" and raw["test_request_id"] != message["original_request_id"]
                    or type(raw.get("test_passed")) is not bool or raw["test_passed"] != (raw["state"] == "approved")
                    or type(observations) is not list or len(observations) > raw["sample_count"]
                    or type(cases) is not list or len(cases) != raw["sample_count"]
                    or any(type(case) is not dict or not _text(case.get("id"), 128) for case in cases)
                    or len({case["id"] for case in cases}) != len(cases)):
                _fail("storage_record_invalid")
            permitted_by_state = ({"employee_package_review"} if raw["state"] == "awaiting_review" else
                                  {"employee_package_publish"} if raw["state"] == "approved" else set())
            if (set(allowed) - permitted_by_state
                    or raw["state"] in {"awaiting_review", "approved", "rejected"} and len(observations) != len(cases)):
                _fail("storage_record_invalid")
            review_request, decision = raw.get("review_request_id"), raw.get("decision")
            if ("review_request_id" not in raw or "decision" not in raw
                    or (review_request is None) != (decision is None)
                    or review_request is not None and (not _valid(review_request, _ID) or decision not in {"approve", "reject"})
                    or raw["state"] == "approved" and decision != "approve"
                    or raw["state"] == "rejected" and decision != "reject"
                    or raw["state"] not in {"approved", "rejected"} and review_request is not None
                    or action == "employee_package_review" and (
                        review_request != message["request_id"] or decision != message["decision"])):
                _fail("storage_record_invalid")
            _plain(cases, 1024 * 1024)
            identities = set()
            for index, row in enumerate(observations):
                if (not _exact(row, {"case_id", "completed", "hard_fail_count", "output", "model"})
                        or not _text(row["case_id"], 128) or row["case_id"] in identities
                        or row["case_id"] != cases[index]["id"]
                        or type(row["completed"]) is not bool
                        or type(row["hard_fail_count"]) is not int or not 0 <= row["hard_fail_count"] <= 1000
                        or type(row["output"]) is not str or len(row["output"].encode("utf-8")) > 65536
                        or row["model"] != result["context"]["model_config"]["model"]):
                    _fail("storage_record_invalid")
                identities.add(row["case_id"])
            if action == "employee_package_test_abandon" or raw["state"] == "abandoned":
                audit = raw.get("abandon")
                if (raw["state"] != "abandoned" or allowed or not _exact(audit, _ABANDON_KEYS)
                        or audit["kind"] != "employee_trial_abandon" or audit["trial_id"] != raw["trial_id"]
                        or audit["spend_reset"] is not False or audit["reviewable"] is not False
                        or audit["publishable"] is not False
                        or action == "employee_package_test_abandon" and (
                            audit["request_id"] != message["request_id"] or audit["reason"] != message["reason"])):
                    _fail("storage_record_invalid")
                _plain(audit, 64 * 1024)
                result["abandon"] = deepcopy(audit)
            result.update(trial_id=raw["trial_id"], state=raw["state"], test_passed=raw["test_passed"],
                          candidate_revision=raw["candidate_revision"], test_request_id=raw["test_request_id"],
                          review_request_id=review_request, decision=decision,
                          cases=deepcopy(cases), observations=deepcopy(observations))
    _plain(result, 2 * 1024 * 1024)
    return result


class EmployeeLifecycleActionDispatcher:
    def __init__(self, *, authenticate, resolve_project, service):
        self.authenticate, self.resolve_project, self.service = authenticate, resolve_project, service

    def _authorize(self, session, project_id, previous_actor=None):
        from .ai_employee_actions import AuthenticatedActor, AuthorizedProject
        actor = self.authenticate(session)
        if (type(actor) is not AuthenticatedActor or not _text(actor.actor, 128)
                or actor.actor.strip() != actor.actor
                or any(ord(char) < 32 or ord(char) == 127 for char in actor.actor)
                or previous_actor is not None and actor != previous_actor):
            _fail("owner_required")
        scope = self.resolve_project(actor, project_id)
        if (type(scope) is not AuthorizedProject or scope.project_id != project_id
                or scope.is_owner is not True):
            _fail("owner_required")
        return actor

    @staticmethod
    def _error(action, project_id, request_id, code, invoked):
        # After any write invocation even an authorization exception can follow
        # a durable mutation. No raw exception or guessed rollback is exposed.
        unknown = invoked or code == "outcome_unknown"
        safe_code = code if type(code) is str and code in _MESSAGES else "outcome_unknown" if invoked else "storage_record_invalid"
        return {"type": "ack", "payload": {"ok": False, "action": action, "project_id": project_id,
            "request_id": request_id, "revision": None,
            "result": "outcome_unknown" if unknown else "unsupported" if safe_code == "unsupported" else "failed",
            "effects_produced": None if invoked else False, "external_effects_produced": None if invoked else False,
            "error": safe_code, "message": _MESSAGES[safe_code],
            "problems": [{"code": safe_code, "severity": "error", "path": "", "message": _MESSAGES[safe_code],
                          "hint": "只读核对原请求；不要自动重发、重新付费或发布。"}], "allowed_actions": []}}

    def dispatch(self, message, *, session):
        action = message.get("type") if type(message) is dict else None
        action = action if type(action) is str and action in SUPPORTED_ACTIONS else ""
        project_id = message.get("project_id") if type(message) is dict else None
        request_id = message.get("request_id") if type(message) is dict else None
        project_id = project_id if _valid(project_id, _ID) else None
        request_id = request_id if _valid(request_id, _ID) else None
        invoked = False
        try:
            request = _request(message)
            actor = self._authorize(session, project_id)
            method = getattr(self.service, _METHODS[action], None)
            if not callable(method) or inspect.iscoroutinefunction(method):
                _fail("unsupported")
            arguments = {key: deepcopy(request[key]) for key in _FIELDS[action]}
            arguments.update({key: deepcopy(request[key]) for key in _OPTIONAL_FIELDS.get(action, set()) if key in request})
            arguments.update(project_id=project_id, actor=actor.actor)
            if action in _WRITES:
                arguments["request_id"] = request_id
            self._authorize(session, project_id, actor)
            invoked = action in _WRITES
            raw = method(**arguments)
            if inspect.isawaitable(raw):
                if inspect.iscoroutine(raw):
                    raw.close()
                _fail("storage_record_invalid")
            self._authorize(session, project_id, actor)
            view = _view(raw, action, request)
            status = view.pop("result", "success")
            effects = view.pop("effects_produced", invoked)
            # A queued test may already be running by the time this ACK arrives.
            # Local admission is confirmed; external/model effects are not zero.
            external = None if action == "employee_package_test_start" else False
            return {"type": "ack", "payload": {**view, "ok": status == "success", "action": action,
                "project_id": project_id, "request_id": request_id, "result": status,
                "effects_produced": effects, "external_effects_produced": external,
                "problems": []}}
        except AIEmployeeContractError as exc:
            return self._error(action, project_id, request_id, exc.code, invoked)
        except Exception:
            return self._error(action, project_id, request_id, "storage_record_invalid", invoked)


def dispatch_lifecycle_action(message, *, service, actor: str, project_id: str, verify):
    """Parent-dispatcher adapter; actor/project/verify are server-owned inputs.

    verify() must synchronously re-read the original authenticated socket and
    active project membership, raise on any change, and return exactly None.
    It must not merely compare JSON fields. The service retains its separate
    current authorization callback for worker dispatch and publication commits.
    """
    def current(_session):
        from .ai_employee_actions import AuthenticatedActor
        if not callable(verify):
            _fail("owner_required")
        result = verify()
        if result is not None:
            if inspect.iscoroutine(result):
                result.close()
            _fail("owner_required")
        return AuthenticatedActor(actor)

    def scope(_actor, requested_project):
        from .ai_employee_actions import AuthorizedProject
        if requested_project != project_id:
            _fail("owner_required")
        return AuthorizedProject(project_id=project_id, is_owner=True)

    return EmployeeLifecycleActionDispatcher(authenticate=current, resolve_project=scope,
                                               service=service).dispatch(message, session=None)
