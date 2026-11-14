"""What a released version did after it was published.

The evidence pack written at publish time answers "what was approved": the
manifest hash, the skill versions, the test records, the approval trail. It
is immutable, and it must stay that way -- rewriting it later would make it
worthless as evidence.

But the question a customer asks is the other one: *this* version, in the
weeks since, how did it actually behave. That evidence accrues afterwards --
canary observations under ``.releases/rollout/``, and the acceptance verdicts
of the orders launched while the version was current, which live in the
business database. Neither belongs inside the signed pack.

So this projection reads both and says plainly which half is which: the
publish-time pack is immutable and hashed; the runtime half is appended
after the fact and carries no chain. Nothing here writes anything.

Plugin-layer module: no engine-kernel changes.
"""

from __future__ import annotations

from typing import Any

ACCEPTANCE_TERMINAL = ("accepted", "accepted_with_exceptions", "rejected")


def rollout_evidence(package: Any) -> dict[str, Any]:
    """The canary ledger for this package's current version, if any."""
    from ampuly.plugins.office_ui.workflow_packages import _load_rollout_observations, _rollout_key

    observations = _load_rollout_observations(package)
    rollout = dict(package.manifest.get("rollout", {}) or {})
    outcomes: dict[str, int] = {}
    for item in observations:
        outcome = str(item.get("outcome", "") or "unknown")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    stamps = sorted(str(item.get("at", "") or "") for item in observations if item.get("at"))
    key = _rollout_key(package)
    return {
        "key": key,
        "status": str(rollout.get("status", "") or ""),
        "observations": len(observations),
        "outcomes": outcomes,
        "first_observed_at": stamps[0] if stamps else "",
        "last_observed_at": stamps[-1] if stamps else "",
        "evidence_files": [str(item.get("evidence_file", "") or "") for item in observations],
        # A ledger is keyed by version *and* candidate fingerprint, so editing
        # the package after a canary leaves the old ledger behind under a
        # different key. Those observations are real and belong to this
        # version; they just do not describe the candidate as it stands now.
        "earlier_candidates": _earlier_candidate_ledgers(package, key),
    }


def _earlier_candidate_ledgers(package: Any, current_key: str) -> list[dict[str, Any]]:
    version = str(package.manifest.get("version", "") or "")
    root = package.root / ".releases" / "rollout"
    if not version or not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        if directory.name == current_key or not directory.name.startswith(f"{version}-"):
            continue
        files = sorted(f.name for f in directory.glob("*.json") if not f.is_symlink())
        if files:
            out.append({"key": directory.name, "observations": len(files)})
    return out


def acceptance_evidence(business: Any, workflow_id: str, version: str) -> dict[str, Any]:
    """Customer verdicts on the orders that ran this version.

    An order records the workflow version when its acceptance criteria are
    frozen, so that event is the join. An order with no verdict yet is
    counted as pending rather than assumed good.
    """
    reader = getattr(business, "acceptance_outcomes_for_version", None)
    if not callable(reader):
        return {"orders": [], "counts": {}, "available": False}
    try:
        rows = list(reader(workflow_id, version) or [])
    except Exception:
        return {"orders": [], "counts": {}, "available": False}
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status", "") or "pending")
        counts[status] = counts.get(status, 0) + 1
    return {
        "orders": rows,
        "counts": counts,
        "settled": sum(counts.get(status, 0) for status in ACCEPTANCE_TERMINAL),
        "available": True,
    }


def build_runtime_evidence(package: Any, business: Any) -> dict[str, Any]:
    """The published pack plus what happened afterwards, kept clearly apart."""
    version = str(package.manifest.get("version", "") or "")
    pack = dict(package.release_evidence)
    return {
        "workflow_id": package.id,
        "version": version,
        "status": package.status,
        # Signed at publish time; hashes in the pack cover exactly this.
        "published_evidence": {
            "manifest_sha256": str(pack.get("manifest_sha256", "") or ""),
            "candidate_fingerprint": str(pack.get("candidate_fingerprint", "") or ""),
            "evidence_file": str(pack.get("evidence_file", "") or ""),
            "evidence_file_sha256": str(pack.get("evidence_file_sha256", "") or ""),
            "test_records": len(pack.get("test_records", []) or []),
            "approval_records": len(pack.get("approval_records", []) or []),
            "change_note": str(pack.get("change_note", "") or ""),
            "rollback_point": str(pack.get("rollback_point", "") or ""),
            "immutable": True,
        },
        # Everything below accrued after publication and is not part of the
        # signed pack; saying so is the point of separating them.
        "runtime_evidence": {
            "rollout": rollout_evidence(package),
            "acceptance": acceptance_evidence(business, package.id, version),
            "immutable": False,
        },
    }
