"""Qualification identity over the effective configuration, never client proof.

The values are supplied by an authenticated server reader. References, not
private knowledge bytes, enter the public reply. Instance trials never qualify
a public package release or another instance. Legacy proofs qualify only the
original empty-parameter, empty-knowledge package context.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import re

from .ai_employee_contract import AIEmployeeContractError, canonical_json

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_INSTANCE = re.compile(r"ei-[a-f0-9]{16}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _fail(code="evaluation_configuration_invalid"):
    raise AIEmployeeContractError(code, "岗位测试配置无法核实；没有沿用旧资格。")


def validate_selection(selection):
    if (type(selection) is not dict or set(selection) != {
            "instance_id", "expected_revision", "expected_fingerprint"}
            or type(selection["instance_id"]) is not str or not _INSTANCE.fullmatch(selection["instance_id"])
            or type(selection["expected_revision"]) is not int or selection["expected_revision"] < 1
            or type(selection["expected_fingerprint"]) is not str
            or not _SHA.fullmatch(selection["expected_fingerprint"])):
        _fail("request_invalid")
    return deepcopy(selection)


def qualification_scope(instance=None):
    if instance is None:
        return {"kind": "package"}
    project, identity = instance.get("project_id"), instance.get("instance_id")
    if (type(project) is not str or not _ID.fullmatch(project)
            or type(identity) is not str or not _INSTANCE.fullmatch(identity)):
        _fail()
    return {"kind": "instance", "project_id": project, "instance_id": identity}


def configuration_fields(*, params, knowledge_refs, scope):
    if type(params) is not dict or type(knowledge_refs) is not list or len(knowledge_refs) > 64:
        _fail()
    if len(canonical_json(params)) > 64 * 1024 or len(canonical_json(knowledge_refs)) > 64 * 1024:
        _fail()
    if scope == {"kind": "package"}:
        if params or knowledge_refs:
            _fail()
    elif (type(scope) is not dict or set(scope) != {"kind", "project_id", "instance_id"}
          or scope.get("kind") != "instance" or scope != qualification_scope(scope)):
        _fail()
    return {"params_fingerprint": digest(params), "knowledge_refs_fingerprint": digest(knowledge_refs),
            "qualification_scope": deepcopy(scope)}


def seal_context(context, *, params, knowledge_refs, scope):
    value = {**deepcopy(context), **configuration_fields(params=params, knowledge_refs=knowledge_refs, scope=scope)}
    checkers = dict(value["checker_versions"])
    checkers.pop("employee_configuration", None)
    payload = {**value, "checker_versions": checkers}
    value["checker_versions"] = {**checkers, "employee_configuration": digest(payload)}
    return value


def context_seal_valid(context):
    checkers = dict(context.get("checker_versions", {}))
    seal = checkers.pop("employee_configuration", None)
    return (type(seal) is str and bool(_SHA.fullmatch(seal))
            and seal == digest({**context, "checker_versions": checkers}))


def context_matches(context, *, model_config, capability_impls, params,
                    knowledge_refs, runtime_profile_fingerprint, instance=None):
    """A matching record must still pass the original evaluation/approval parser."""
    if instance is None and (params or knowledge_refs):
        return False
    fields = configuration_fields(params=params, knowledge_refs=knowledge_refs,
                                  scope=qualification_scope(instance))
    if any(context.get(key) != expected for key, expected in (
            ("model_config", model_config), ("capability_impls", capability_impls),
            ("runtime_profile_fingerprint", runtime_profile_fingerprint),
            ("params_fingerprint", fields["params_fingerprint"]))):
        return False
    if "qualification_scope" not in context:
        # Old package trials had no instance reader and only supported {}.
        return not params and not knowledge_refs and context.get("params_fingerprint") == digest({})
    if not context_seal_valid(context):
        return False
    scope = context["qualification_scope"]
    if scope == {"kind": "package"}:
        return not params and not knowledge_refs and context.get("knowledge_refs_fingerprint") == digest([])
    return all(context.get(key) == value for key, value in fields.items())
