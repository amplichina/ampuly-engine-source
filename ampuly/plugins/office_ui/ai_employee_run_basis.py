"""Internal, read-only recovery of an ORIGINAL frozen employee run basis.

The host supplies the authenticated identity, independently retained digests,
old envelope/pins and two already-authorized server callbacks. Bind those
callbacks to EmployeeInstanceRepository.read_revision and
PublishedEmployeeCatalog.read, never to a client-selected provider or detail /
current / latest resolver. This module constructs no repository or filesystem.

The catalog callback's real contract verifies immutable file bytes and whole
Skill trees before returning. No manifest hash declaration or self-reported
"files_verified" bit can substitute for that source. Retired versions remain
readable for their original pins. Optional current-tail verification is NOT
new-adoption authority here. This result is private internal data, not a WS
receipt, READY determination, access grant, live-human decision or run admission.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Any, Protocol

from .ai_employee_contract import (
    AIEmployeeContractError, canonical_json, validate_package_manifest,
)
from .ai_employee_instance_contract import validate_instance
from .ai_employee_run_contract import FrozenEmployeePins, restore_employee_pins
from .ai_employee_run_envelope import (
    EmployeeRunIdentity, FrozenEmployeeRunEnvelope, restore_employee_run_envelope,
)

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_CATALOG_FIELDS = frozenset({
    "package", "publication", "fingerprint", "retired", "existing_pins_usable",
    "new_adoption_allowed", "is_current_verified", "problems", "effects_produced",
    "revision", "status", "source", "allowed_actions",
})
_PUBLICATION_FIELDS = frozenset({
    "version", "package_fingerprint", "status", "at", "actor",
    "previous_sha256", "record_sha256",
})


class InstanceRevisionSource(Protocol):
    def __call__(self, project_id: str, instance_id: str, *, revision: int,
                 revision_fingerprint: str, package_fingerprint: str) -> dict[str, Any]: ...


class PinnedPackageSource(Protocol):
    def __call__(self, package_id: str, *, version: str, fingerprint: str,
                 purpose: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True, repr=False)
class EmployeeRunBasisSources:
    """Server-owned capabilities; constructing this carrier does not authorize.

    The host must bind both callbacks to the same already-authorized project /
    order / step / attempt and enforce fresh private-scope access as required.
    Neither callback is received or deserialized from a wire message.
    """
    read_instance_revision: InstanceRevisionSource
    read_pinned_package: PinnedPackageSource


@dataclass(frozen=True, slots=True, repr=False)
class FrozenEmployeeRunBasis:
    """Detached private snapshots; never serialize this object as a public ACK.

    Digests validate structure/integrity, not permission. Consumers still need
    live guards for runtime, references, grants, money, human duty and effects.
    Raw values are deliberately excluded from repr and generic to_dict APIs.
    """
    identity: EmployeeRunIdentity
    envelope_fingerprint: str
    pins_fingerprint: str
    catalog_revision: str
    publication_status: str
    is_current_index_verified: bool
    _instance_raw: bytes
    _package_raw: bytes
    _pin_raw: bytes
    _envelope_raw: bytes

    @property
    def validation_scope(self) -> str:
        return "frozen_basis_only"

    def instance_snapshot(self) -> dict[str, Any]:
        return json.loads(self._instance_raw)

    def package_manifest(self) -> dict[str, Any]:
        return json.loads(self._package_raw)

    def pin(self) -> dict[str, Any]:
        return json.loads(self._pin_raw)

    def envelope(self) -> dict[str, Any]:
        return json.loads(self._envelope_raw)


def _fail(code: str, message: str) -> None:
    # Only fixed public text. No source exception, finding path, private ref,
    # source repr, or exception chain is placed in an observable error.
    raise AIEmployeeContractError(code, message) from None


def _sha(value: Any) -> bool:
    return type(value) is str and bool(_SHA.fullmatch(value))


def _identity(identity: Any) -> dict[str, Any]:
    if type(identity) is not EmployeeRunIdentity:
        _fail("employee_run_scope_invalid", "需要完整明确的原运行关联身份。")
    try:
        identity.__post_init__()
        return {field.name: getattr(identity, field.name) for field in fields(EmployeeRunIdentity)}
    except Exception:
        pass
    _fail("employee_run_scope_invalid", "原运行关联身份无效。")


def _source_read(source: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return source(*args, **kwargs)
    except Exception as exc:
        # Preserve only the outcome-unknown category, with fresh fixed text;
        # all other low-level refusal/corruption/not-found information is private.
        unknown = isinstance(exc, AIEmployeeContractError) and exc.code == "outcome_unknown"
        code = "outcome_unknown" if unknown else "employee_run_basis_unavailable"
    _fail(code, "原冻结依据读取未能确认；不能改取当前记录。")


def _snapshot(value: Any, code: str) -> tuple[dict[str, Any], bytes]:
    try:
        if type(value) is not dict:
            _fail(code, "原冻结依据不是完整有界对象。")
        raw = canonical_json(value)
        return json.loads(raw), raw
    except Exception:
        pass
    _fail(code, "原冻结依据不是完整有界对象。")


def _exact_instance(instance: dict[str, Any], pin: dict[str, Any], project_id: str,
                    package: dict[str, Any] | None = None) -> None:
    if instance.get("project_id") != project_id:
        _fail("employee_run_basis_conflict", "实例依据不匹配原冻结范围。")
    for name in ("instance_id", "instance_revision", "revision_fingerprint",
                 "package_id", "package_version", "package_fingerprint"):
        if name not in instance or canonical_json(instance[name]) != canonical_json(pin[name]):
            _fail("employee_run_basis_conflict", "实例依据不匹配原固定修订及包。")
    if validate_instance(instance, package, expected_project_id=project_id):
        _fail("employee_run_basis_invalid", "原实例修订未满足完整性及结构合同。")


def _catalog_package(view: dict[str, Any], pin: dict[str, Any]) -> dict[str, Any]:
    if set(view) != _CATALOG_FIELDS:
        _fail("employee_run_basis_invalid", "固定包读取缺少实际目录视图事实。")
    package, publication = view["package"], view["publication"]
    status = view["status"]
    if (type(package) is not dict or type(publication) is not dict
            or type(status) is not str or status not in {"published", "retired"}
            or view["source"] != "published" or view["effects_produced"] is not False
            or view["existing_pins_usable"] is not True or not _sha(view["revision"])):
        _fail("employee_run_basis_invalid", "固定包目录视图不能确认原在途依据。")
    if (type(view["retired"]) is not bool or view["retired"] != (status == "retired")
            or type(view["is_current_verified"]) is not bool
            or type(view["new_adoption_allowed"]) is not bool
            or view["new_adoption_allowed"] != (status == "published" and view["is_current_verified"])
            or view["allowed_actions"] != ["employee_package_list", "employee_package_detail"]
            or type(view["problems"]) is not list):
        _fail("employee_run_basis_invalid", "固定包目录事实互相冲突。")
    expected_record_fields = _PUBLICATION_FIELDS | ({"reason"} if status == "retired" else set())
    if (set(publication) != expected_record_fields or publication["status"] != status
            or publication["version"] != pin["package_version"]
            or publication["package_fingerprint"] != pin["package_fingerprint"]
            or not _sha(publication["previous_sha256"]) or not _sha(publication["record_sha256"])
            or any(type(publication[name]) is not str or not publication[name]
                   for name in ("at", "actor"))
            or status == "retired" and (type(publication["reason"]) is not str
                                       or not publication["reason"].strip()
                                       or len(publication["reason"]) > 4096)):
        _fail("employee_run_basis_invalid", "固定包发布索引事实无效。")
    record_body = {key: value for key, value in publication.items() if key != "record_sha256"}
    if hashlib.sha256(canonical_json(record_body)).hexdigest() != publication["record_sha256"]:
        _fail("employee_run_basis_invalid", "固定包发布索引事实无效。")
    if (package.get("package_id") != pin["package_id"]
            or package.get("version") != pin["package_version"]
            or package.get("package_fingerprint") != pin["package_fingerprint"]
            or view["fingerprint"] != pin["package_fingerprint"]
            or package.get("status") != "published"):
        _fail("employee_run_basis_conflict", "包依据不匹配原固定身份、版本及指纹。")
    problems = validate_package_manifest(package, require_complete=True)
    if any(row["severity"] == "error" and row["code"] != "employee_package_capability_unverified"
           for row in problems):
        _fail("employee_run_basis_invalid", "固定包未满足完整结构合同。")
    # No manifest-only package_fingerprint recomputation: declared files and
    # trees are actually read/verified by the trusted catalog source, not here.
    return package


def _pin_references(pin: dict[str, Any], instance: dict[str, Any], package: dict[str, Any]) -> None:
    skill_trees = {row["skill_name"]: row["tree_fingerprint"] for row in package.get("skills", [])}
    knowledge: dict[str, str] = {}
    for row in instance.get("knowledge_refs", []):
        if row["ref"] in knowledge and knowledge[row["ref"]] != row["version"]:
            _fail("employee_run_basis_conflict", "旧实例引用不匹配原固定席位。")
        knowledge[row["ref"]] = row["version"]
    humans = {slot: row["employee_id"] for slot, row in instance.get("human_assignments", {}).items()}
    for name, expected in (("skill_tree_sha256", skill_trees), ("knowledge_versions", knowledge),
                           ("human_assignments", humans)):
        if name not in pin:
            if expected:
                _fail("employee_run_basis_conflict", "原固定席位缺少对应的冻结引用。")
        elif canonical_json(pin[name]) != canonical_json(expected):
            _fail("employee_run_basis_conflict", "旧实例及包引用不匹配原固定席位。")
    # Check no declared frozen cap widens known original package/instance caps.
    # Do NOT recalculate caps from current platform/order state or fill keys.
    for layer in (package["loop_policy"], instance.get("limits", {})):
        for name, value in layer.items():
            if name not in pin["effective_limits"]:
                _fail("employee_run_basis_conflict", "原固定上限缺少已声明的依据。")
            actual = pin["effective_limits"][name]
            if (Decimal(actual) > Decimal(value) if name == "max_cost_cny" else actual > value):
                _fail("employee_run_basis_conflict", "原固定上限放宽了旧依据。")


def read_employee_run_basis(envelope: FrozenEmployeeRunEnvelope, *,
                            expected_identity: EmployeeRunIdentity,
                            pins: FrozenEmployeePins,
                            expected_envelope_fingerprint: str,
                            expected_pins_fingerprint: str,
                            sources: EmployeeRunBasisSources,
                            previous: FrozenEmployeeRunEnvelope | None = None) -> FrozenEmployeeRunBasis:
    """Restore exact old private records after all explicit identity checks.

    The caller owns authentication and provenance of expected identity/digests /
    pins / previous and callbacks. A digest read from, or freshly calculated
    over, the same untrusted object is NOT an independent retained anchor.
    attempt>1 requires its trusted predecessor; missing history fails closed.
    """
    associated = _identity(expected_identity)
    # Retain a detached identity as well as detached private JSON snapshots.
    expected_identity = EmployeeRunIdentity(**associated)
    if type(envelope) is not FrozenEmployeeRunEnvelope:
        _fail("employee_run_basis_invalid", "需要原完整冻结运行对象。")
    if canonical_json(_identity(envelope.identity)) != canonical_json(associated):
        _fail("employee_run_scope_invalid", "原运行对象不属于所要求的完整关联身份。")
    if (type(pins) is not FrozenEmployeePins or pins.project_id != expected_identity.project_id
            or pins.workflow_id != expected_identity.workflow_id):
        _fail("project_scope_denied", "原固定席位不属于所要求的项目流程。")
    if (not _sha(expected_envelope_fingerprint) or not _sha(expected_pins_fingerprint)
            or envelope.fingerprint != expected_envelope_fingerprint
            or pins.fingerprint != expected_pins_fingerprint):
        _fail("fingerprint_conflict", "缺少或不匹配独立保留的原依据摘要。")
    invalid_frozen = False
    try:
        old_pins = restore_employee_pins(pins.to_bytes(), project_id=expected_identity.project_id,
            workflow_id=expected_identity.workflow_id, expected_fingerprint=expected_pins_fingerprint)
        old_envelope = restore_employee_run_envelope(envelope.to_bytes(), identity=expected_identity,
            pins=old_pins, expected_fingerprint=expected_envelope_fingerprint, previous=previous)
        pin = old_pins.for_node(expected_identity.node_id)
    except Exception:
        invalid_frozen = True
    if invalid_frozen:
        _fail("employee_run_basis_invalid", "原冻结字节、席位或返修链不能完成验证。")
    if (type(sources) is not EmployeeRunBasisSources
            or not callable(sources.read_instance_revision) or not callable(sources.read_pinned_package)):
        _fail("employee_run_basis_source_missing", "缺少已鉴权且限定范围的原依据读取源。")
    # Mapping is intentional: repository keyword revision <- pin.instance_revision.
    returned_instance = _source_read(sources.read_instance_revision,
        expected_identity.project_id, pin["instance_id"], revision=pin["instance_revision"],
        revision_fingerprint=pin["revision_fingerprint"], package_fingerprint=pin["package_fingerprint"])
    instance, instance_raw = _snapshot(returned_instance, "employee_run_basis_invalid")
    instance_error_code = None
    try:
        _exact_instance(instance, pin, expected_identity.project_id)
    except Exception as exc:
        # Our fixed messages remain safe; never propagate lower-level details.
        instance_error_code = "employee_run_basis_conflict" if isinstance(exc, AIEmployeeContractError) and exc.code == "employee_run_basis_conflict" else "employee_run_basis_invalid"
    if instance_error_code is not None:
        _fail(instance_error_code, "旧实例不能确认原固定修订及完整性。")
    returned_view = _source_read(sources.read_pinned_package, pin["package_id"],
        version=pin["package_version"], fingerprint=pin["package_fingerprint"], purpose="pinned")
    view, _ = _snapshot(returned_view, "employee_run_basis_invalid")
    package_error_code = None
    try:
        package = _catalog_package(view, pin)
        _exact_instance(instance, pin, expected_identity.project_id, package)
        _pin_references(pin, instance, package)
        package_raw = canonical_json(package)
    except Exception as exc:
        package_error_code = "employee_run_basis_conflict" if isinstance(exc, AIEmployeeContractError) and exc.code == "employee_run_basis_conflict" else "employee_run_basis_invalid"
    if package_error_code is not None:
        _fail(package_error_code, "旧实例及固定包不能确认一致的原冻结依据。")
    return FrozenEmployeeRunBasis(expected_identity, expected_envelope_fingerprint,
        expected_pins_fingerprint, view["revision"], view["status"], view["is_current_verified"],
        instance_raw, package_raw, canonical_json(pin), old_envelope.to_bytes())
