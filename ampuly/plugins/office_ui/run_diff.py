"""Why this run and that run were not the same.

"It worked last time" is the most common report and the least actionable,
because the thing that changed is usually not the thing anyone remembers
changing. Each launch pins a contract -- the workflow and its version, the
skills and their versions, and the digests of both -- so two runs can be
compared on what they actually ran instead of on recollection.

Three rules keep the answer honest:

**A missing contract is not agreement.** A run launched before contracts
existed, or one whose snapshot did not survive, cannot be compared. That is
reported as "we cannot tell what that run ran", never as "no differences".

**Same version with a different digest is the loudest finding.** It means the
label stayed still while the content moved: "v0.2.1" meant two different
things on two days, and every comparison that trusted the version number was
wrong. This ranks above an ordinary version change, which at least announced
itself.

**Nothing recorded differs is not "the runs are identical".** The contract
does not cover the model, the provider, the customer's own inputs or the state
of the world. The phrase this module uses says only what it checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Ordered worst first: a caller rendering only the top line still shows the
# finding that matters most.
SEVERITY = ("edited_in_place", "changed", "added", "removed", "unknown")


@dataclass(frozen=True)
class Difference:
    """One thing that was not the same, and how alarming that is."""

    subject: str
    kind: str
    before: str = ""
    after: str = ""
    note: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "subject": self.subject, "kind": self.kind,
            "before": self.before, "after": self.after, "note": self.note,
        }


@dataclass
class RunComparison:
    comparable: bool = True
    reason: str = ""
    differences: list[Difference] = field(default_factory=list)

    @property
    def edited_in_place(self) -> list[Difference]:
        return [d for d in self.differences if d.kind == "edited_in_place"]

    def to_payload(self) -> dict[str, Any]:
        ordered = sorted(
            self.differences,
            key=lambda d: (SEVERITY.index(d.kind) if d.kind in SEVERITY else len(SEVERITY), d.subject),
        )
        return {
            "comparable": self.comparable,
            "reason": self.reason,
            "differences": [d.to_payload() for d in ordered],
            "edited_in_place": [d.subject for d in self.edited_in_place],
            # Deliberately not "identical": this compares what was recorded,
            # and the contract does not cover the model, the provider, the
            # customer's inputs or the state of the world.
            "summary": (
                "nothing that was recorded differs" if self.comparable and not self.differences
                else self.reason if not self.comparable
                else f"{len(self.differences)} difference(s) in what was recorded"
            ),
        }


def _contract(source: Any) -> dict[str, Any]:
    """The pinned snapshot, from a task, its metadata, or the contract itself."""
    if not isinstance(source, dict):
        source = dict(getattr(source, "metadata", {}) or {})
    if "workflow_contract" in source:
        return dict(source.get("workflow_contract") or {})
    return dict(source or {})


def compare_runs(before: Any, after: Any, *, before_label: str = "before", after_label: str = "after") -> RunComparison:
    """What differs between two launches, by what each one pinned."""
    left, right = _contract(before), _contract(after)
    missing = [label for label, contract in ((before_label, left), (after_label, right)) if not contract]
    if missing:
        return RunComparison(
            comparable=False,
            reason=(
                f"no contract was recorded for {', '.join(missing)}, so what it ran cannot be established; "
                "this is not the same as the two runs agreeing"
            ),
        )

    differences: list[Difference] = []
    left_version = str(left.get("workflow_version", "") or "")
    right_version = str(right.get("workflow_version", "") or "")
    left_digest = str(left.get("manifest_sha256", "") or "")
    right_digest = str(right.get("manifest_sha256", "") or "")
    if str(left.get("workflow_id", "")) != str(right.get("workflow_id", "")):
        differences.append(Difference(
            subject="workflow", kind="changed",
            before=str(left.get("workflow_id", "")), after=str(right.get("workflow_id", "")),
            note="a different workflow entirely",
        ))
    elif left_version != right_version:
        differences.append(Difference(
            subject="workflow version", kind="changed", before=left_version, after=right_version,
        ))
    elif left_digest and right_digest and left_digest != right_digest:
        differences.append(Difference(
            subject=f"workflow {left_version or 'version'}", kind="edited_in_place",
            before=left_digest[:12], after=right_digest[:12],
            note="the version stayed the same and the content did not: this version meant two different things",
        ))

    left_skills = {str(k): str(v) for k, v in dict(left.get("skill_versions", {}) or {}).items()}
    right_skills = {str(k): str(v) for k, v in dict(right.get("skill_versions", {}) or {}).items()}
    left_hashes = {str(k): str(v) for k, v in dict(left.get("skill_sha256", {}) or {}).items()}
    right_hashes = {str(k): str(v) for k, v in dict(right.get("skill_sha256", {}) or {}).items()}
    for skill in sorted(set(left_skills) | set(right_skills)):
        if skill not in right_skills:
            differences.append(Difference(subject=f"skill {skill}", kind="removed", before=left_skills[skill]))
            continue
        if skill not in left_skills:
            differences.append(Difference(subject=f"skill {skill}", kind="added", after=right_skills[skill]))
            continue
        if left_skills[skill] != right_skills[skill]:
            differences.append(Difference(
                subject=f"skill {skill}", kind="changed",
                before=left_skills[skill], after=right_skills[skill],
            ))
            continue
        left_hash, right_hash = left_hashes.get(skill, ""), right_hashes.get(skill, "")
        if left_hash and right_hash and left_hash != right_hash:
            differences.append(Difference(
                subject=f"skill {skill} {left_skills[skill]}", kind="edited_in_place",
                before=left_hash[:12], after=right_hash[:12],
                note="same version, different content",
            ))
    return RunComparison(differences=differences)
