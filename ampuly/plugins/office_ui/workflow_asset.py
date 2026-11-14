"""Read-only qualification of field-proven workflow assets.

The asset is not another mutable record.  Publication identity belongs to the
workflow package and customer verdicts belong to the business event store.
This module projects those two authorities into an answer to one question:
"what has this exact workflow version actually proved in customer delivery?"

No projection is shareable by default.  Information-security controls do not
grant intellectual-property redistribution rights; export authorization is a
separate, later gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .release_runtime_evidence import build_runtime_evidence


_SHA256 = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class WorkflowAssetPolicy:
    """Conservative local qualification thresholds, not sharing permission."""

    repeat_accepted_orders: int = 3
    repeat_distinct_customers: int = 2

    def __post_init__(self) -> None:
        if self.repeat_accepted_orders < 2:
            raise ValueError("repeat verification needs at least two accepted orders")
        if self.repeat_distinct_customers < 2:
            raise ValueError("repeat verification needs at least two distinct customers")


@dataclass(frozen=True)
class WorkflowAssetProjection:
    workflow_id: str
    version: str
    candidate_fingerprint: str
    manifest_sha256: str
    level: str
    accepted_orders: int
    distinct_customers: int
    acceptance_counts: dict[str, int] = field(default_factory=dict)
    rollout_status: str = ""
    blockers: tuple[str, ...] = ()
    evidence_digest: str = ""
    # Accepted orders whose customer explicitly authorized redistributing this exact version.
    authorized_orders: int = 0

    @property
    def qualified(self) -> bool:
        return self.level in {"customer_verified", "repeat_verified"}

    @property
    def shareable(self) -> bool:
        """Only a qualified version with at least one explicit, exactly-bound customer grant."""
        return self.qualified and self.authorized_orders >= 1

    @property
    def sharing_blocker(self) -> str:
        if self.shareable:
            return ""
        if self.authorized_orders >= 1:
            return "authorized_version_not_qualified"
        return "explicit_redistribution_authorization_required"

    def to_payload(self) -> dict[str, Any]:
        """Safe projection: no customer names, ids, order ids, notes or paths."""

        return {
            "workflow_id": self.workflow_id,
            "version": self.version,
            "candidate_fingerprint": self.candidate_fingerprint,
            "manifest_sha256": self.manifest_sha256,
            "level": self.level,
            "qualified": self.qualified,
            "accepted_orders": self.accepted_orders,
            "distinct_customers": self.distinct_customers,
            "acceptance_counts": dict(self.acceptance_counts),
            "rollout_status": self.rollout_status,
            "blockers": list(self.blockers),
            "evidence_digest": self.evidence_digest,
            # Never inferred from acceptance: true only when a customer's explicit grant names this exact
            # version, candidate fingerprint and manifest digest, and the version is itself qualified.
            "authorized_orders": self.authorized_orders,
            "shareable": self.shareable,
            "sharing_blocker": self.sharing_blocker,
        }


def project_workflow_asset(
    package: Any,
    business: Any,
    *,
    policy: WorkflowAssetPolicy | None = None,
) -> WorkflowAssetProjection:
    """Classify one exact released workflow version from authoritative facts."""

    policy = policy or WorkflowAssetPolicy()
    evidence = build_runtime_evidence(package, business)
    return project_workflow_asset_from_evidence(evidence, policy=policy)


def project_workflow_asset_from_evidence(
    evidence: dict[str, Any],
    *,
    policy: WorkflowAssetPolicy | None = None,
) -> WorkflowAssetProjection:
    """Classify an already scope-narrowed release-evidence projection.

    WebSocket callers must narrow customer-visible acceptance rows first and
    only then call this function.  That prevents aggregate counts from
    revealing verdicts belonging to another customer scope.
    """

    policy = policy or WorkflowAssetPolicy()
    published = dict(evidence.get("published_evidence", {}) or {})
    runtime = dict(evidence.get("runtime_evidence", {}) or {})
    acceptance = dict(runtime.get("acceptance", {}) or {})
    rollout = dict(runtime.get("rollout", {}) or {})
    rows = [dict(row) for row in list(acceptance.get("orders", []) or []) if isinstance(row, dict)]
    counts = {
        str(key): int(value)
        for key, value in dict(acceptance.get("counts", {}) or {}).items()
        if isinstance(value, int) and value >= 0
    }
    accepted_rows = [row for row in rows if str(row.get("status", "")) == "accepted"]
    # A grant counts only on an accepted order, and only when it names exactly what was published.
    authorized_rows = [
        row for row in accepted_rows
        if any(
            str(grant.get("candidate_fingerprint", "") or "").lower() == str(published.get("candidate_fingerprint", "") or "").strip().lower()
            and str(grant.get("manifest_sha256", "") or "").lower() == str(published.get("manifest_sha256", "") or "").strip().lower()
            for grant in list(row.get("redistribution", []) or [])
            if isinstance(grant, dict)
        )
    ]
    distinct_customers = len({
        str(row.get("customer_id", "")).strip()
        for row in accepted_rows
        if str(row.get("customer_id", "")).strip()
    })
    workflow_id = str(evidence.get("workflow_id", "") or "").strip()
    version = str(evidence.get("version", "") or "").strip()
    fingerprint = str(published.get("candidate_fingerprint", "") or "").strip().lower()
    manifest_sha = str(published.get("manifest_sha256", "") or "").strip().lower()

    blockers: list[str] = []
    if str(evidence.get("status", "") or "") != "published":
        blockers.append("workflow_version_not_published")
    if not version:
        blockers.append("workflow_version_missing")
    if not _SHA256.fullmatch(fingerprint):
        blockers.append("candidate_fingerprint_missing_or_invalid")
    if not _SHA256.fullmatch(manifest_sha):
        blockers.append("manifest_sha256_missing_or_invalid")
    if acceptance.get("available") is not True:
        blockers.append("acceptance_evidence_unavailable")
    if not accepted_rows:
        blockers.append("no_fully_accepted_customer_order")
    if counts.get("rejected", 0):
        blockers.append("customer_rejection_present")
    if counts.get("accepted_with_exceptions", 0):
        blockers.append("acceptance_exceptions_present")
    if counts.get("pending", 0) or counts.get("submitted", 0):
        blockers.append("customer_acceptance_pending")

    level = "observed"
    hard_blockers = [item for item in blockers if item != "customer_acceptance_pending"]
    if not hard_blockers:
        level = "customer_verified"
        if (
            len(accepted_rows) >= policy.repeat_accepted_orders
            and distinct_customers >= policy.repeat_distinct_customers
            and not blockers
        ):
            level = "repeat_verified"

    digest_payload = {
        "workflow_id": workflow_id,
        "version": version,
        "candidate_fingerprint": fingerprint,
        "manifest_sha256": manifest_sha,
        "acceptance_counts": counts,
        "accepted_orders": len(accepted_rows),
        "distinct_customers": distinct_customers,
        "rollout_status": str(rollout.get("status", "") or ""),
        "blockers": blockers,
        "authorized_orders": len(authorized_rows),
    }
    evidence_digest = hashlib.sha256(json.dumps(
        digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return WorkflowAssetProjection(
        workflow_id=workflow_id,
        version=version,
        candidate_fingerprint=fingerprint,
        manifest_sha256=manifest_sha,
        level=level,
        accepted_orders=len(accepted_rows),
        distinct_customers=distinct_customers,
        acceptance_counts=counts,
        rollout_status=str(rollout.get("status", "") or ""),
        blockers=tuple(blockers),
        evidence_digest=evidence_digest,
        authorized_orders=len(authorized_rows),
    )
