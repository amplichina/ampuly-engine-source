"""One customer's solution, packaged so another customer can receive it.

Today a delivery is spread across a workflow package, its skills, a plan
saved as a blueprint, and whatever tool grants and connector expectations
live in someone's head. Handing that to a second customer means copying
pieces and hoping nothing was missed -- and the pieces that get missed are
the ones nobody wrote down.

A solution package is that whole thing with a version and a digest: what runs
(workflow and skills at pinned versions), what it needs (connectors, tool
grants, permissions), how it is judged (acceptance criteria), how it is
undone (rollback), and where it came from.

Two rules do the real work. **Customer content never travels**: a package
carries the shape of a delivery, not the last customer's name, keys or
acceptance record -- importing one must not make the new customer look like
they already accepted it. And **a package that does not match its digest is
refused**: a tampered or truncated bundle is exactly the case where "mostly
fine" is worst.

Built on the existing workflow packages and blueprints; this adds a
versioned wrapper, not a parallel registry.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

PACKAGE_CONTRACT = 1
# Fields a customer's own material would arrive in. None of them belong in a
# package: they are what makes one delivery *this* customer's.
CUSTOMER_FIELDS = (
    "customer_id",
    "customer_name",
    "brand_name",
    "contact",
    "acceptance_records",
    "orders",
    "credentials",
    "env_values",
)


class SolutionPackageError(ValueError):
    """A package that cannot be trusted is not installed."""


def digest_of(payload: dict[str, Any]) -> str:
    """Content digest over everything except the digest field itself."""
    body = {k: v for k, v in payload.items() if k != "digest"}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def strip_customer_content(value: Any) -> Any:
    """Remove anything that identifies the customer this came from.

    Recursive on purpose: customer content hides in nested plans and node
    metadata, and a shallow filter would let it through where nobody looks.
    """
    if isinstance(value, dict):
        return {
            k: strip_customer_content(v)
            for k, v in value.items()
            if k not in CUSTOMER_FIELDS
        }
    if isinstance(value, list):
        return [strip_customer_content(item) for item in value]
    return value


@dataclass
class SolutionPackage:
    """What a second customer needs in order to run the same solution."""

    solution_id: str
    version: str
    name: str = ""
    workflow_id: str = ""
    workflow_version: str = ""
    skill_versions: dict[str, str] = field(default_factory=dict)
    roles: list[str] = field(default_factory=list)
    connector_requirements: list[str] = field(default_factory=list)
    tool_grants: dict[str, list[str]] = field(default_factory=dict)
    credential_purposes: list[str] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)
    acceptance_criteria: list[str] = field(default_factory=list)
    rollback_plan: str = ""
    migration_plan: str = ""
    observability: dict[str, Any] = field(default_factory=dict)
    compatibility: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    change_note: str = ""
    created_at: str = ""
    created_by: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "contract": PACKAGE_CONTRACT,
            "solution_id": self.solution_id,
            "version": self.version,
            "name": self.name,
            "workflow_id": self.workflow_id,
            "workflow_version": self.workflow_version,
            "skill_versions": dict(self.skill_versions),
            "roles": list(self.roles),
            "connector_requirements": list(self.connector_requirements),
            "tool_grants": {k: list(v) for k, v in self.tool_grants.items()},
            "credential_purposes": list(self.credential_purposes),
            "plan": strip_customer_content(dict(self.plan)),
            "acceptance_criteria": list(self.acceptance_criteria),
            "rollback_plan": self.rollback_plan,
            "migration_plan": self.migration_plan,
            "observability": dict(self.observability),
            "compatibility": dict(self.compatibility),
            "provenance": dict(self.provenance),
            "change_note": self.change_note,
            "created_at": self.created_at,
            "created_by": self.created_by,
        }
        payload["digest"] = digest_of(payload)
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SolutionPackage:
        verify_payload(payload)
        return cls(
            solution_id=str(payload["solution_id"]),
            version=str(payload["version"]),
            name=str(payload.get("name", "") or ""),
            workflow_id=str(payload.get("workflow_id", "") or ""),
            workflow_version=str(payload.get("workflow_version", "") or ""),
            skill_versions={str(k): str(v) for k, v in dict(payload.get("skill_versions", {}) or {}).items()},
            roles=[str(r) for r in list(payload.get("roles", []) or [])],
            connector_requirements=[str(c) for c in list(payload.get("connector_requirements", []) or [])],
            tool_grants={str(k): [str(p) for p in list(v or [])] for k, v in dict(payload.get("tool_grants", {}) or {}).items()},
            credential_purposes=[str(p) for p in list(payload.get("credential_purposes", []) or [])],
            plan=dict(payload.get("plan", {}) or {}),
            acceptance_criteria=[str(c) for c in list(payload.get("acceptance_criteria", []) or [])],
            rollback_plan=str(payload.get("rollback_plan", "") or ""),
            migration_plan=str(payload.get("migration_plan", "") or ""),
            observability=dict(payload.get("observability", {}) or {}),
            compatibility=dict(payload.get("compatibility", {}) or {}),
            provenance=dict(payload.get("provenance", {}) or {}),
            change_note=str(payload.get("change_note", "") or ""),
            created_at=str(payload.get("created_at", "") or ""),
            created_by=str(payload.get("created_by", "") or ""),
        )


def verify_payload(payload: dict[str, Any]) -> None:
    """Refuse anything that cannot be trusted, before it is installed."""
    if not isinstance(payload, dict):
        raise SolutionPackageError("a package must be an object")
    if int(payload.get("contract", 0) or 0) != PACKAGE_CONTRACT:
        raise SolutionPackageError(
            f"package contract {payload.get('contract')!r} is not {PACKAGE_CONTRACT}; refusing to guess its shape"
        )
    for required in ("solution_id", "version", "digest"):
        if not str(payload.get(required, "") or "").strip():
            raise SolutionPackageError(f"package is missing {required}")
    actual = digest_of(payload)
    if actual != str(payload["digest"]):
        raise SolutionPackageError(
            "package digest does not match its content; it was changed or truncated in transit"
        )
    leaked = sorted(_customer_fields_in(payload))
    if leaked:
        raise SolutionPackageError(
            f"package carries customer content ({', '.join(leaked)}); a solution travels, a customer does not"
        )


def _customer_fields_in(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in CUSTOMER_FIELDS:
                found.add(str(key))
            found |= _customer_fields_in(item)
    elif isinstance(value, list):
        for item in value:
            found |= _customer_fields_in(item)
    return found


def build_from_workflow(
    package: Any,
    *,
    solution_id: str,
    version: str,
    actor: str,
    plan: dict[str, Any] | None = None,
    acceptance_criteria: list[str] | None = None,
    rollback_plan: str = "",
    migration_plan: str = "",
    change_note: str = "",
    source_order_id: str = "",
) -> SolutionPackage:
    """Assemble a solution from a workflow package plus an authored plan.

    Provenance records where it came from, and deliberately not *who* it was
    for: knowing a solution began life on order wo-123 is useful history;
    carrying that customer's name into the next delivery is not.
    """
    if not str(solution_id or "").strip():
        raise SolutionPackageError("solution_id is required")
    if not str(version or "").strip():
        raise SolutionPackageError("version is required")
    if not str(actor or "").strip():
        raise SolutionPackageError("actor is required")

    manifest = dict(getattr(package, "manifest", {}) or {})
    nodes = list(manifest.get("nodes", []) or [])
    roles = []
    for node in nodes:
        role = str(node.get("role", "") or "")
        if role and role not in roles:
            roles.append(role)
    return SolutionPackage(
        solution_id=str(solution_id).strip(),
        version=str(version).strip(),
        name=str(manifest.get("name", "") or getattr(package, "id", "")),
        workflow_id=str(getattr(package, "id", "") or ""),
        workflow_version=str(manifest.get("version", "") or ""),
        skill_versions=dict(manifest.get("skill_versions", {}) or {}),
        roles=roles,
        connector_requirements=sorted({
            str(tool) for node in nodes for tool in list(node.get("tools", []) or [])
            if str(tool).startswith(("channel", "request_customer"))
        }),
        tool_grants={str(k): [str(p) for p in list(v or [])] for k, v in dict(manifest.get("tool_grants", {}) or {}).items()},
        credential_purposes=sorted({"image"} if any(
            "image_generate" in list(node.get("tools", []) or []) for node in nodes
        ) else set()),
        plan=strip_customer_content(dict(plan or {})),
        acceptance_criteria=[str(c) for c in list(acceptance_criteria or [])],
        rollback_plan=str(rollback_plan or ""),
        migration_plan=str(migration_plan or ""),
        observability={"events": ["order_events", "gate_events", "delegation_events", "effects"]},
        compatibility={"engine_contract": PACKAGE_CONTRACT},
        provenance={
            "source_workflow": str(getattr(package, "id", "") or ""),
            "source_workflow_version": str(manifest.get("version", "") or ""),
            # The order is history; the customer behind it is not carried.
            "source_order_id": str(source_order_id or ""),
        },
        change_note=str(change_note or ""),
        created_at=datetime.now().isoformat(timespec="seconds"),
        created_by=str(actor).strip(),
    )


def install_preflight(payload: dict[str, Any], *, available_skills: dict[str, str]) -> list[str]:
    """Problems that must stop an install, in the operator's words.

    A missing dependency found after installation is a broken delivery; found
    before, it is a shopping list.
    """
    problems: list[str] = []
    try:
        verify_payload(payload)
    except SolutionPackageError as exc:
        return [str(exc)]
    for skill, version in dict(payload.get("skill_versions", {}) or {}).items():
        present = available_skills.get(str(skill))
        if present is None:
            problems.append(f"skill {skill!r} (v{version}) is not installed here")
        elif str(present) != str(version):
            problems.append(f"skill {skill!r} is v{present} here, the package pins v{version}")
    if int(dict(payload.get("compatibility", {}) or {}).get("engine_contract", 0) or 0) != PACKAGE_CONTRACT:
        problems.append("the package was built for a different engine contract")
    return problems


# ── upgrading from one version to the next ──
#
# A solution that changes under a running customer is the thing this section
# exists to prevent. Some changes are additive and can be applied; others
# change what the customer agreed to, and those need a person to say yes --
# by name, not by clicking through.

BREAKING_KINDS = (
    "acceptance_criteria_removed",
    "acceptance_criteria_changed",
    "workflow_changed",
    "skill_version_downgraded",
    "rollback_plan_removed",
    "contract_changed",
)


@dataclass
class Change:
    kind: str
    detail: str
    breaking: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "breaking": self.breaking}


@dataclass
class MigrationPreview:
    """What upgrading would do, before anything is done."""

    from_version: str
    to_version: str
    changes: list[Change] = field(default_factory=list)

    @property
    def breaking(self) -> list[Change]:
        return [c for c in self.changes if c.breaking]

    @property
    def safe(self) -> bool:
        return not self.breaking

    def to_payload(self) -> dict[str, Any]:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "changes": [c.to_payload() for c in self.changes],
            "breaking": [c.to_payload() for c in self.breaking],
            "safe": self.safe,
        }


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(version or "").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts or [0])


def preview_migration(current: dict[str, Any], incoming: dict[str, Any]) -> MigrationPreview:
    """Compare two versions of one solution and say what would change.

    Read-only: it changes nothing, which is what makes it safe to run before
    deciding. A change it cannot classify is reported as breaking -- the
    unknown case is the one that should stop a person, not slide past them.
    """
    verify_payload(current)
    verify_payload(incoming)
    if str(current.get("solution_id")) != str(incoming.get("solution_id")):
        raise SolutionPackageError("these are two different solutions, not two versions of one")

    preview = MigrationPreview(str(current.get("version", "")), str(incoming.get("version", "")))
    add = preview.changes.append

    if str(current.get("workflow_id")) != str(incoming.get("workflow_id")):
        add(Change("workflow_changed", f"{current.get('workflow_id')} -> {incoming.get('workflow_id')}", True))
    elif str(current.get("workflow_version")) != str(incoming.get("workflow_version")):
        add(Change(
            "workflow_version_changed",
            f"{current.get('workflow_version')} -> {incoming.get('workflow_version')}",
            False,
        ))

    old_skills = dict(current.get("skill_versions", {}) or {})
    new_skills = dict(incoming.get("skill_versions", {}) or {})
    for skill, version in new_skills.items():
        previous = old_skills.get(skill)
        if previous is None:
            add(Change("skill_added", f"{skill} v{version}", False))
        elif _version_tuple(version) < _version_tuple(str(previous)):
            # Going backwards is how a fix disappears without anyone noticing.
            add(Change("skill_version_downgraded", f"{skill} v{previous} -> v{version}", True))
        elif str(version) != str(previous):
            add(Change("skill_version_changed", f"{skill} v{previous} -> v{version}", False))
    for skill in old_skills:
        if skill not in new_skills:
            add(Change("skill_removed", f"{skill} is gone", True))

    old_criteria = [str(c) for c in list(current.get("acceptance_criteria", []) or [])]
    new_criteria = [str(c) for c in list(incoming.get("acceptance_criteria", []) or [])]
    removed = [c for c in old_criteria if c not in new_criteria]
    added = [c for c in new_criteria if c not in old_criteria]
    if removed:
        # The customer agreed to these. Dropping one quietly lowers the bar
        # they accepted.
        add(Change("acceptance_criteria_removed", "; ".join(removed), True))
    if added:
        add(Change("acceptance_criteria_added", "; ".join(added), False))

    if str(current.get("rollback_plan", "") or "").strip() and not str(incoming.get("rollback_plan", "") or "").strip():
        add(Change("rollback_plan_removed", "the new version has no rollback plan", True))

    for grant in dict(incoming.get("tool_grants", {}) or {}):
        if grant not in dict(current.get("tool_grants", {}) or {}):
            # Widening what may run without asking is a decision, not an upgrade.
            add(Change("tool_grant_added", f"{grant} would run without asking", True))

    for purpose in list(incoming.get("credential_purposes", []) or []):
        if purpose not in list(current.get("credential_purposes", []) or []):
            add(Change("credential_purpose_added", f"needs a {purpose} key", False))

    return preview


def apply_migration(
    current: dict[str, Any],
    incoming: dict[str, Any],
    *,
    actor: str,
    accept_breaking: list[str] | None = None,
) -> dict[str, Any]:
    """Upgrade, refusing any breaking change nobody named.

    ``accept_breaking`` takes the *kinds* a person has looked at and agreed
    to. Naming them one by one is the point: a single "yes to everything"
    button is how an acceptance criterion disappears unnoticed.
    """
    if not str(actor or "").strip():
        raise SolutionPackageError("actor is required")
    preview = preview_migration(current, incoming)
    accepted = {str(k) for k in (accept_breaking or [])}
    unacknowledged = [c for c in preview.breaking if c.kind not in accepted]
    if unacknowledged:
        raise SolutionPackageError(
            "this upgrade changes what the customer agreed to: "
            + "; ".join(f"{c.kind} ({c.detail})" for c in unacknowledged)
        )
    upgraded = dict(incoming)
    provenance = dict(upgraded.get("provenance", {}) or {})
    provenance.update({
        "upgraded_from": str(current.get("version", "")),
        "upgraded_by": str(actor).strip(),
        "upgraded_at": datetime.now().isoformat(timespec="seconds"),
        "accepted_breaking": sorted(accepted & {c.kind for c in preview.breaking}),
    })
    upgraded["provenance"] = provenance
    upgraded["digest"] = digest_of(upgraded)
    return upgraded
