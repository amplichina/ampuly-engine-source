"""Trusted dispatch of the managed CS adapter over a frozen synthetic taskset.

The model emits only the inner response. Context, citations, effect/result
envelope and handoff are produced by the existing ConversationService. Public
payloads never choose an adapter or claim authenticated customer identities.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import json
import math
import re

from .assets import build_shipped_customer_service_package
from .core.contracts import Context
from .empty_knowledge import EmptyKnowledgeBase
from .package_builder import MANAGED_FIELDS
from .service import CustomerServiceDraftSession

EXECUTOR_VERSION = "customer-service-conversation-draft-v1"
UNDERSTANDING_EXECUTOR_VERSION = "customer-service-conversation-draft-v2"
_EDITABLE_FILES = frozenset({"conversation-system.md", "conversation-playbook.md",
                           "skills/customer-service/SKILL.md"})
_EDITABLE_FIELDS = frozenset({"package_id", "version", "name", "responsibility",
                            "scope_in", "scope_out", "checklist", "skills"})


def _fail(code):
    from ampuly.plugins.office_ui.ai_employee_contract import AIEmployeeContractError
    raise AIEmployeeContractError(code, "客服岗位测试停止；未伪造身份、知识、交付或执行结果。") from None


def _canonical(value):
    from ampuly.plugins.office_ui.ai_employee_contract import canonical_json
    return canonical_json(value)


def _has_understanding(manifest):
    # A routing hint only: executor_profile must still compare every fixed
    # contract and source hash against the matching known factory blueprint.
    try:
        return "understanding" in manifest["output_schema"]["properties"]["draft"]["properties"]
    except (KeyError, TypeError):
        return False


def _blueprint(manifest):
    try:
        amount = manifest["loop_policy"]["max_cost_cny"]
        return build_shipped_customer_service_package(actor="managed-profile",
            created_at="2026-10-16T00:00:00+00:00", max_cost_cny=amount,
            structured_understanding=_has_understanding(manifest))
    except Exception:
        _fail("customer_service_profile_unavailable")


def _is_cs_shape(manifest):
    return (type(manifest) is dict and type(manifest.get("output_schema")) is dict
        and manifest["output_schema"].get("properties", {}).get("kind", {}).get("const")
            == "ampuly.cs.conversation-draft/1")


def executor_profile(manifest):
    """Stable server implementation identity; not a permission or test result.

The upstream source revision anchors the managed adapter lineage. Editable
prompt/playbook/Skill content is separately bound by the current package and
tree fingerprints and must be retested; it need not equal factory prose.
    """
    if type(manifest) is not dict:
        _fail("customer_service_profile_invalid")
    provenance = manifest.get("provenance", {})
    if type(provenance) is not dict or provenance.get("source_repo") != "ampuly-customer-service-lab":
        if _is_cs_shape(manifest):
            _fail("customer_service_executor_unregistered")
        return {"kind": "native", "version": "employee-native-v1"}
    baseline = _blueprint(manifest).manifest
    if provenance.get("source_revision") != baseline["provenance"]["source_revision"]:
        _fail("customer_service_source_unregistered")
    ignored = MANAGED_FIELDS | _EDITABLE_FIELDS
    for key in (set(manifest) | set(baseline)) - ignored:
        if _canonical(manifest.get(key)) != _canonical(baseline.get(key)):
            _fail("customer_service_profile_mismatch")
    skills = manifest.get("skills")
    if (type(skills) is not list or len(skills) != 1 or type(skills[0]) is not dict
            or skills[0].get("skill_name") != "customer-service" or skills[0].get("source") != "package"
            or not re.fullmatch(r"[a-f0-9]{64}", str(skills[0].get("tree_fingerprint", "")))):
        _fail("customer_service_skill_mismatch")
    fixed = {key: baseline[key] for key in ("input_schema", "output_schema", "model_profile",
        "artifact_contract", "capability_requirements", "knowledge_protocol") if key in baseline}
    return {"kind": "customer_service", "version": (
        UNDERSTANDING_EXECUTOR_VERSION if _has_understanding(manifest) else EXECUTOR_VERSION),
        "upstream_source_revision": provenance["source_revision"],
        "fixed_contract_fingerprint": hashlib.sha256(_canonical(fixed)).hexdigest(),
        "knowledge_mode": "host_explicit_empty_scope", "channel": "console"}


def _snapshot(manifest, files):
    from ampuly.plugins.office_ui.employee_publication_contents import publication_content_fingerprint
    profile = executor_profile(manifest)
    if profile["kind"] != "customer_service":
        _fail("customer_service_executor_unregistered")
    expected = _blueprint(manifest).files
    if (type(files) is not dict or set(files) != set(expected)
            or any(type(raw) is not bytes for raw in files.values())
            or sum(map(len, files.values())) > 1024 * 1024):
        _fail("customer_service_snapshot_invalid")
    for path in set(files) - _EDITABLE_FILES:
        if files[path] != expected[path]:
            _fail("customer_service_contract_changed")
    if publication_content_fingerprint(manifest, files) != manifest.get("package_fingerprint"):
        _fail("customer_service_snapshot_conflict")
    try:
        parts = [files[path].decode("utf-8") for path in (
            "conversation-system.md", "conversation-playbook.md", "skills/customer-service/SKILL.md")]
    except UnicodeError:
        _fail("customer_service_prompt_invalid")
    prompt = "\n\n".join(parts)
    if not prompt.strip() or len(prompt.encode("utf-8")) > 32000:
        _fail("customer_service_prompt_invalid")
    return profile, prompt


def resolve_trial_execution(manifest, files, trial):
    from ampuly.plugins.office_ui.employee_trial_runner import TrialExecution
    profile = executor_profile(manifest)
    if profile["kind"] == "native":
        return TrialExecution("native")
    _snapshot(manifest, files)
    if not _taskset_scope(trial):
        _fail("customer_service_trial_scope_invalid")
    return TrialExecution("customer_service", run_case=run_case)


def _taskset_scope(trial):
    return (type(trial) is dict and type(trial.get("candidate")) is dict
            and trial["candidate"].get("data_scope") == "package_taskset")


async def _checkpoint(checkpoint):
    if not callable(checkpoint):
        _fail("customer_service_policy_unavailable")
    value = checkpoint()
    if inspect.isawaitable(value):
        value = await value
    if value is False:
        _fail("customer_service_policy_denied")


async def run_case(*, manifest, files, sample, trial, task, provider, checkpoint, timeout_seconds):
    from ampuly.plugins.office_ui.employee_trial_runner import TrialCaseResult
    _, prompt = _snapshot(manifest, files)
    if (not _taskset_scope(trial)
            or type(trial.get("trial_id")) is not str or not trial["trial_id"]
            or type(sample) is not dict or type(sample.get("id")) is not str
            or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 30):
        _fail("customer_service_trial_scope_invalid")
    frozen = json.loads(files["taskset.json"])
    cases = [row for row in frozen["cases"] if row.get("id") == sample["id"]]
    if len(cases) != 1 or cases[0] != sample or frozen.get("mode") != "fictional-offline":
        _fail("customer_service_case_scope_invalid")
    if (getattr(task, "project_id", None) != trial.get("project_id")
            or task.metadata.get("employee_trial_id") != trial["trial_id"]
            or task.metadata.get("employee_trial_case_id") != sample["id"]
            or not task.metadata.get("budget_id")):
        _fail("customer_service_trial_scope_invalid")
    if set(sample) - {"id", "title", "turns", "signals", "expected"}:
        _fail("customer_service_case_scope_invalid")
    identity = hashlib.sha256(_canonical([trial["trial_id"], sample["id"]])).hexdigest()[:24]
    context = Context(tenant_id="synthetic-trial", shop_id="empty-knowledge-scope",
        seller_id="synthetic-seller", buyer_id="unverified-" + identity,
        conversation_id="trial-" + identity, channel="console", account_id="synthetic-account")
    cap = getattr(getattr(provider, "config", None), "max_tokens", None)
    if type(cap) is not int or not 1 <= cap <= 3500:
        _fail("customer_service_output_cap_invalid")
    await _checkpoint(checkpoint)
    session = CustomerServiceDraftSession(context=context, knowledge=EmptyKnowledgeBase(context),
        provider=provider, before_dispatch=checkpoint, task_type="customer_service_employee_trial",
        system_prompt=prompt, max_output_tokens=cap,
        structured_understanding=_has_understanding(manifest))
    output = await session.prepare(deepcopy(sample["turns"]), signals=deepcopy(sample.get("signals", {})),
                                   timeout_seconds=timeout_seconds)
    await _checkpoint(checkpoint)
    # Governance errors propagate from the original session/adapter. A safety
    # fallback remains visible but is not successful model-quality evidence.
    hard = int(output.get("result_status") != "draft" or output.get("mode") != "model-candidate"
        or output.get("validation_errors") != [] or output.get("model_calls") != 1
        or output.get("effects_produced") != [] or output.get("review_required") is not True
        or output.get("customer_claims_verified") is not False or output.get("sources") != [])
    return TrialCaseResult(output=output, completed=True, hard_fail_count=hard)
