"""Candidates: compiled from a sealed demo, admitted by the owner through the product, kept on disk (4.3 X07, third slice).

A candidate is compiled only from a sealed demonstration on disk and carries
its own digest; the owner admits it through the console, which is the only
caller of the product's admission authority; every external-effect step needs
an approval reference (a checkpoint the owner answered) before the candidate
may run at all. Both are written under the product's own directory and read
back with their digests checked. Nothing here replays anything: a run still
needs a binding to an admission, a scope and an Effect claim (G12).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ampuly import __version__
from ampuly.computer_use.demo_recorder import demos_dir, load_demo
from ampuly.computer_use.workflow_contracts import (
    ADMISSION_AUTHORITY,
    CandidateAdmission,
    CandidateStep,
    CompiledCandidate,
    WorkflowRefused,
    admit_candidate,
    compile_candidate,
)
from ampuly.core.config import replace_with_retry

COMPILER_VERSION = f"superones-compiler {__version__}"
SIDECAR_SUFFIXES = (".admission.json", ".repair.json")
_CANDIDATE_SUFFIXES = (".json", *SIDECAR_SUFFIXES)


def candidates_dir() -> Path:
    from ampuly.core.config import get_ampuly_home

    return get_ampuly_home() / "computer_use" / "candidates"


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(name or ""))[:120] or "candidate"


def candidate_path(candidate_id: str, directory: Path | None = None, *, suffix: str = ".json") -> Path:
    """Resolve one plain candidate id inside the candidate store.

    Candidate ids are identities, not paths.  Refuse aliases instead of
    silently rewriting them: dots would collide with sidecar suffixes, while
    separators or drive/UNC syntax could escape the store on Windows.
    """
    clean = str(candidate_id or "").strip()
    if not clean or "." in clean or clean != _safe(clean):
        raise WorkflowRefused("candidate_id_invalid", clean[:80])
    if suffix not in _CANDIDATE_SUFFIXES:
        raise ValueError("unsupported candidate suffix")
    root = Path(directory) if directory is not None else candidates_dir()
    return root / f"{clean}{suffix}"


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    replace_with_retry(tmp, path)
    return path


# ── candidate persistence ─────────────────────────────────────────────────

def candidate_to_payload(candidate: CompiledCandidate) -> dict[str, Any]:
    return {"candidate_id": candidate.candidate_id, "demo_id": candidate.demo_id, "demo_seal": candidate.demo_seal,
            "scope_digest": candidate.scope_digest, "compiler_version": candidate.compiler_version, "digest": candidate.digest,
            "steps": [{**s.__dict__, "verification_sources": list(s.verification_sources)} for s in candidate.steps]}


def candidate_from_payload(payload: dict[str, Any]) -> CompiledCandidate:
    steps = tuple(CandidateStep(sequence=int(s["sequence"]), kind=str(s["kind"]), target=dict(s.get("target", {}) or {}),
                                expected_before_hash=str(s.get("expected_before_hash", "")), expected_after_hash=str(s.get("expected_after_hash", "")),
                                verification_sources=tuple(str(v) for v in s.get("verification_sources", [])), intent=str(s.get("intent", "") or ""))
                  for s in list(payload.get("steps", []) or []))
    return CompiledCandidate(candidate_id=str(payload.get("candidate_id", "") or ""), demo_id=str(payload.get("demo_id", "") or ""),
                             demo_seal=str(payload.get("demo_seal", "") or ""), scope_digest=str(payload.get("scope_digest", "") or ""),
                             steps=steps, compiler_version=str(payload.get("compiler_version", "") or ""), digest=str(payload.get("digest", "") or ""))


def save_candidate(candidate: CompiledCandidate, directory: Path | None = None) -> Path:
    if not candidate.intact:
        raise WorkflowRefused("candidate_digest_changed", candidate.candidate_id)
    return _write(candidate_path(candidate.candidate_id, directory), candidate_to_payload(candidate))


def load_candidate(path: Path | str) -> CompiledCandidate:
    candidate = candidate_from_payload(json.loads(Path(path).read_text(encoding="utf-8")))
    if not candidate.intact:
        raise WorkflowRefused("candidate_digest_changed", str(path))
    return candidate


# ── admission persistence ─────────────────────────────────────────────────

def admission_to_payload(admission: CandidateAdmission) -> dict[str, Any]:
    return {"admission_id": admission.admission_id, "candidate_id": admission.candidate_id, "candidate_digest": admission.candidate_digest,
            "scope_digest": admission.scope_digest, "admitted_by": admission.admitted_by, "authority": admission.authority,
            "approval_refs": {str(k): v for k, v in admission.approval_refs.items()}}


def admission_from_payload(payload: dict[str, Any]) -> CandidateAdmission:
    return CandidateAdmission(admission_id=str(payload.get("admission_id", "") or ""), candidate_id=str(payload.get("candidate_id", "") or ""),
                              candidate_digest=str(payload.get("candidate_digest", "") or ""), scope_digest=str(payload.get("scope_digest", "") or ""),
                              admitted_by=str(payload.get("admitted_by", "") or ""), authority=str(payload.get("authority", "") or ""),
                              approval_refs={int(k): str(v) for k, v in dict(payload.get("approval_refs", {}) or {}).items()})


def save_admission(admission: CandidateAdmission, directory: Path | None = None) -> Path:
    return _write(candidate_path(admission.candidate_id, directory, suffix=".admission.json"), admission_to_payload(admission))


def load_admission(candidate_id: str, directory: Path | None = None) -> CandidateAdmission | None:
    path = candidate_path(candidate_id, directory, suffix=".admission.json")
    if not path.is_file():
        return None
    return admission_from_payload(json.loads(path.read_text(encoding="utf-8")))


# ── the console's two verbs ───────────────────────────────────────────────

def compile_from_demo(demo_id: str, *, demos: Path | None = None, candidates: Path | None = None) -> dict[str, Any]:
    """Compile the sealed demo on disk into a candidate on disk. A demo that is not there, or not sealed, is refused."""
    demo_path = (Path(demos) if demos is not None else demos_dir()) / f"{_safe(demo_id)}.json"
    if not demo_path.is_file():
        raise WorkflowRefused("demo_not_found", demo_id)
    demo = load_demo(demo_path)
    demo_identity = str(demo.demo_id or "").strip()
    if not demo_identity or "." in demo_identity or demo_identity != _safe(demo_identity):
        raise WorkflowRefused("demo_id_invalid", demo_identity[:80])
    candidate = compile_candidate(demo, candidate_id=f"cand-{demo.demo_id}", compiler_version=COMPILER_VERSION)
    path = save_candidate(candidate, candidates)
    external = [s.sequence for s in candidate.steps if s.risk.value == "external_effect"]
    return {"candidate_id": candidate.candidate_id, "demo_id": demo.demo_id, "digest": candidate.digest, "steps": len(candidate.steps),
            "kinds": [s.kind for s in candidate.steps], "external_effect_steps": external, "saved_to": str(path),
            "compiler_version": COMPILER_VERSION}


def admit_from_console(candidate_id: str, *, admitted_by: str, approval_refs: dict[int, str] | None = None,
                       approval_facts: Mapping[str, Mapping[str, Any]] | None = None,
                       candidates: Path | None = None) -> dict[str, Any]:
    """The owner admits a candidate on disk. The console is the only caller of the product's authority;
    the candidate is re-read and its digest checked; external-effect steps need their approval references."""
    directory = Path(candidates) if candidates is not None else candidates_dir()
    path = candidate_path(candidate_id, directory)
    if not path.is_file():
        raise WorkflowRefused("candidate_not_found", candidate_id)
    candidate = load_candidate(path)
    refs = dict(approval_refs or {})
    # Keep the contract's missing-reference refusal first: it is more useful
    # than saying facts were not supplied for a reference that is absent.
    missing = [step.sequence for step in candidate.steps if step.risk.value == "external_effect" and not refs.get(step.sequence)]
    if missing:
        raise WorkflowRefused("external_effect_step_without_approval", ",".join(str(item) for item in missing))
    effect_steps = {step.sequence: step for step in candidate.steps if step.risk.value == "external_effect"}
    if any(sequence not in effect_steps for sequence in refs):
        raise WorkflowRefused("approval_ref_on_non_effect_step")
    values = [str(value or "") for value in refs.values()]
    if len(set(values)) != len(values):
        raise WorkflowRefused("approval_ref_reused")
    if refs and approval_facts is None:
        raise WorkflowRefused("approval_refs_unverified")
    facts_by_ref = dict(approval_facts or {})
    for sequence, ref in refs.items():
        facts = dict(facts_by_ref.get(str(ref), {}) or {})
        if not facts:
            raise WorkflowRefused("approval_ref_not_found", str(ref))
        if str(facts.get("kind", "") or "") != "browser.submit" or facts.get("authorized") is not True:
            raise WorkflowRefused("approval_ref_not_approved", str(ref))
        if str(facts.get("observation_hash", "") or "") != effect_steps[sequence].expected_before_hash:
            raise WorkflowRefused("approval_ref_for_another_step", str(ref))
    admission = admit_candidate(candidate, admission_id=f"adm-{candidate.candidate_id}", admitted_by=str(admitted_by or "owner"),
                                authority=ADMISSION_AUTHORITY, approval_refs=refs)
    saved = save_admission(admission, directory)
    return {"admission_id": admission.admission_id, "candidate_id": candidate.candidate_id, "candidate_digest": candidate.digest,
            "admitted_by": admission.admitted_by, "approval_refs": {str(k): v for k, v in admission.approval_refs.items()},
            "saved_to": str(saved)}


def list_candidates(candidates: Path | None = None) -> list[dict[str, Any]]:
    """What is on disk and whether each candidate is intact and admitted (the admission still covering it)."""
    directory = Path(candidates) if candidates is not None else candidates_dir()
    rows = []
    if not directory.is_dir():
        return rows
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(SIDECAR_SUFFIXES):
            continue
        try:
            candidate = candidate_from_payload(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            rows.append({"candidate_id": path.stem, "intact": False, "admitted": False, "problem": "unreadable"})
            continue
        if candidate.candidate_id != path.stem:
            rows.append({"candidate_id": path.stem, "intact": False, "admitted": False, "problem": "not_a_candidate"})
            continue
        admission = load_admission(candidate.candidate_id, directory)
        rows.append({"candidate_id": candidate.candidate_id, "demo_id": candidate.demo_id, "steps": len(candidate.steps),
                     "intact": candidate.intact, "admitted": bool(admission and admission.covers(candidate)),
                     "admitted_by": admission.admitted_by if admission else "",
                     # Which steps reach outside the page: an admission has to cite an approval for each of them, and
                     # the compile receipt was the only place that said which -- gone after a reload.
                     "external_effect_steps": [s.sequence for s in candidate.steps if s.risk.value == "external_effect"]})
    return rows
