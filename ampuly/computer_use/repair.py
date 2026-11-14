"""A repair is a new candidate with its difference against the admitted one spelled out (4.3 X07, sixth slice; C20).

A replay that stopped on drift is not patched in place: the owner records a
fresh demonstration on the page as it is now, it is compiled like any other,
and the repair record says step by step what changed -- the same, retargeted,
a page that reads differently, a step added or removed -- and whether any
external effect changed hands, because that needs a fresh approval reference
before the repair may be admitted. The admitted candidate stays as it is
until the owner admits the repair through the same verb as any candidate.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ampuly.computer_use.candidates import candidate_path, candidates_dir, compile_from_demo, load_admission, load_candidate
from ampuly.computer_use.contracts import ACTION_KINDS
from ampuly.computer_use.workflow_contracts import ActionClass, CandidateStep, CompiledCandidate, WorkflowRefused
from ampuly.core.config import replace_with_retry

# Kinds whose change of hands in a repair needs a fresh approval reference.
ACTION_EXTERNAL = frozenset(kind for kind, risk in ACTION_KINDS.items() if risk is ActionClass.EXTERNAL_EFFECT)


def _step_change(old: CandidateStep | None, new: CandidateStep | None) -> dict[str, Any]:
    # Whether an external effect is party to this step on either side: such a change needs a fresh approval.
    external = bool((old is not None and old.kind in ACTION_EXTERNAL) or (new is not None and new.kind in ACTION_EXTERNAL))
    if old is None:
        return {"change": "added", "kind": new.kind, "target": dict(new.target), "external": external}
    if new is None:
        return {"change": "removed", "kind": old.kind, "target": dict(old.target), "external": external}
    if old.kind != new.kind:
        return {"change": "kind_changed", "from": old.kind, "to": new.kind, "target": dict(new.target), "external": external}
    if dict(old.target) != dict(new.target):
        return {"change": "retargeted", "kind": new.kind, "from": dict(old.target), "to": dict(new.target), "external": external}
    if old.expected_before_hash != new.expected_before_hash or old.expected_after_hash != new.expected_after_hash:
        return {"change": "page_changed", "kind": new.kind, "target": dict(new.target), "external": external}
    return {"change": "same", "kind": new.kind, "external": external}


def candidate_diff(old: CompiledCandidate, new: CompiledCandidate) -> dict[str, Any]:
    """Step by step, what the repair does differently from the admitted candidate."""
    length = max(len(old.steps), len(new.steps))
    steps = []
    for index in range(length):
        before = old.steps[index] if index < len(old.steps) else None
        after = new.steps[index] if index < len(new.steps) else None
        steps.append({"sequence": index + 1, **_step_change(before, after)})
    counts: dict[str, int] = {}
    for row in steps:
        counts[row["change"]] = counts.get(row["change"], 0) + 1
    effects_changed = [row["sequence"] for row in steps if row["change"] != "same" and row["external"]]
    return {
        "old_candidate": old.candidate_id, "new_candidate": new.candidate_id, "steps": steps, "counts": counts,
        "only_pages_changed": all(row["change"] in {"same", "page_changed"} for row in steps) and len(old.steps) == len(new.steps),
        "external_effect_steps_changed": effects_changed,
        "needs_fresh_approval": bool(effects_changed),
    }


def propose_repair(*, old_candidate_id: str, demo_id: str, stopped_reason: str = "", candidates: Path | None = None,
                   demos: Path | None = None) -> dict[str, Any]:
    """Compile the new demo into a candidate and write the repair record next to it. Nothing is admitted."""
    directory = Path(candidates) if candidates is not None else candidates_dir()
    old_path = candidate_path(old_candidate_id, directory)
    if not old_path.is_file():
        raise WorkflowRefused("candidate_not_found", old_candidate_id)
    old = load_candidate(old_path)
    admission = load_admission(old_candidate_id, directory)
    compiled = compile_from_demo(demo_id, demos=demos, candidates=directory)
    new = load_candidate(Path(compiled["saved_to"]))
    if new.candidate_id == old.candidate_id:
        raise WorkflowRefused("repair_is_the_same_candidate", new.candidate_id)
    diff = candidate_diff(old, new)
    record = {
        "repairs": old.candidate_id, "repaired_by": new.candidate_id, "old_admitted": bool(admission and admission.covers(old)),
        "stopped_reason": str(stopped_reason or "")[:200], "diff": diff,
        "carried_approval_refs": {str(k): v for k, v in (admission.approval_refs if admission else {}).items()
                                  if int(k) not in diff["external_effect_steps_changed"]},
    }
    path = candidate_path(new.candidate_id, directory, suffix=".repair.json")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    replace_with_retry(tmp, path)
    return {**record, "saved_to": str(path), "steps": len(new.steps)}


def record_rerun(candidate_id: str, outcome: dict[str, Any], *, directory: Path | None = None) -> dict[str, Any] | None:
    """The original acceptance, run again against the repair: every replay of a repair candidate is written
    into its repair record with where it ended, so 'repaired' is a run that completed, not a compile that
    succeeded. Returns the updated record, or None when the candidate is not a repair."""
    from datetime import datetime, timezone

    path = candidate_path(candidate_id, directory, suffix=".repair.json")
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    reruns = list(record.get("acceptance_reruns", []) or [])
    reruns.append({
        "run_id": str(outcome.get("run_id", "") or ""), "status": str(outcome.get("status", "") or ""),
        "reason": str(outcome.get("reason", "") or ""), "at_step": outcome.get("at_step"),
        "steps_performed": len(list(outcome.get("performed", []) or [])),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    record["acceptance_reruns"] = reruns[-20:]
    record["acceptance_passed"] = any(r["status"] == "complete" for r in reruns)
    record["last_rerun"] = reruns[-1]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    replace_with_retry(tmp, path)
    return record


def load_repair(candidate_id: str, directory: Path | None = None) -> dict[str, Any] | None:
    path = candidate_path(candidate_id, directory, suffix=".repair.json")
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
