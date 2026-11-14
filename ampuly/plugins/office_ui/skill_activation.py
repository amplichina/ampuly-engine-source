"""Staged, recoverable activation for evaluated Skill replacements."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ampuly.core.config import replace_with_retry
from ampuly.core.file_lock import lock_file_descriptor, unlock_file_descriptor
from ampuly.plugins.office_ui.skill_replacement import (
    SkillReplacementCandidate,
    load_skill_replacement_candidate,
    skill_replacement_evaluation_blocker,
    skill_tree_fingerprint,
)


class SkillActivationError(ValueError):
    pass


@dataclass(frozen=True)
class SkillActivation:
    root: Path
    manifest: dict[str, Any]

    @property
    def status(self) -> str:
        return str(self.manifest.get("status", ""))

    @property
    def fingerprint(self) -> str:
        return str(self.manifest.get("deployment_fingerprint", ""))

    def to_payload(self) -> dict[str, str]:
        """Console-safe identity; local roots and evidence contents stay private."""

        return {
            "candidate_id": str(self.manifest.get("candidate_id", "")),
            "candidate_fingerprint": str(self.manifest.get("candidate_fingerprint", "")),
            "skill_id": str(self.manifest.get("skill_id", "")),
            "deployment_fingerprint": self.fingerprint,
            "status": self.status,
            "staged_at": str(self.manifest.get("staged_at", "")),
            "activated_at": str(self.manifest.get("activated_at", "")),
            "rolled_back_at": str(self.manifest.get("rolled_back_at", "")),
        }


_STATES = frozenset({"staged", "activating", "active", "rolling_back", "rolled_back"})
_EVIDENCE_FILES = ("candidate.json", "test-result.json", "evaluation-spec.json", "evaluation-report.json", "review.json")


def project_skill_activation_root(ampuly_home: Path, project_id: str) -> Path:
    project = str(project_id or "").strip()
    if not project or len(project) > 256:
        raise SkillActivationError("project_id is required")
    key = hashlib.sha256(project.encode("utf-8")).hexdigest()[:24]
    return Path(ampuly_home).resolve() / "skill_activations" / "projects" / key


def skill_activation_by_id(root: Path, candidate_id: str) -> SkillActivation:
    base = Path(root).resolve()
    target = _child(base, str(candidate_id or "").strip(), "activation")
    if not target.is_dir():
        raise SkillActivationError("skill activation does not exist")
    return load_skill_activation(target)


def list_skill_activations(root: Path) -> list[SkillActivation]:
    """Load every deployment strictly so damaged activation evidence is visible as failure."""

    base = Path(root).resolve()
    if not base.exists():
        return []
    if not base.is_dir() or _linked(base):
        raise SkillActivationError("skill activation store is unsafe")
    deployments: list[SkillActivation] = []
    for child in sorted(base.iterdir(), key=lambda item: item.name):
        if child.name in {".locks"} or child.name.startswith("."):
            continue
        if not child.is_dir() or _linked(child):
            raise SkillActivationError("skill activation store contains an unsafe entry")
        deployments.append(load_skill_activation(child))
    return sorted(
        deployments,
        key=lambda deployment: (
            str(deployment.manifest.get("staged_at", "")),
            str(deployment.manifest.get("candidate_id", "")),
        ),
        reverse=True,
    )


def stage_skill_activation(
    candidate: SkillReplacementCandidate,
    *,
    skill_library_root: Path,
    deployment_root: Path,
    actor: str,
) -> SkillActivation:
    """Seal old/new trees and approval evidence without touching live Skill."""

    candidate = load_skill_replacement_candidate(candidate.root)
    if candidate.status != "approved":
        raise SkillActivationError("only an approved skill candidate can be staged")
    if not candidate.manifest.get("evaluation_report_sha256"):
        raise SkillActivationError("skill activation requires a bound dual-run evaluation")
    report = _json(candidate.root / "evaluation-report.json", "evaluation report")
    if str((report.get("evaluation") or {}).get("verdict", "")) != "passed":
        raise SkillActivationError("skill activation requires a passed dual-run evaluation")
    # 4.7-A03: the passed comparison must be of this exact candidate (spec asset, report fingerprint), the same
    # rule approval applies -- so an approval recorded before that rule existed cannot stage on another's run.
    blocker = skill_replacement_evaluation_blocker(candidate)
    if blocker:
        raise SkillActivationError(blocker.replace("不能批准", "不能暂存"))
    skill_id = str(candidate.manifest.get("skill_id", "") or "")
    live = _child(Path(skill_library_root), skill_id, "live skill")
    if not live.is_dir():
        raise SkillActivationError("live skill does not exist")
    observed = skill_tree_fingerprint(live)
    expected = str(candidate.manifest.get("current_fingerprint", "") or "")
    if observed != expected:
        raise SkillActivationError("live skill changed after the improvement proposal")
    root = _child(Path(deployment_root), candidate.candidate_id, "activation")
    if root.exists():
        deployment = load_skill_activation(root)
        if deployment.manifest.get("candidate_fingerprint") != candidate.fingerprint:
            raise SkillActivationError("activation id belongs to another candidate")
        return deployment
    base = Path(deployment_root).resolve()
    base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{candidate.candidate_id}-", dir=base))
    try:
        shutil.copytree(live, staging / "previous")
        shutil.copytree(live, staging / "next")
        shutil.copy2(candidate.root / "SKILL.md", staging / "next" / "SKILL.md")
        (staging / "next" / "tests").mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate.root / "tests" / "contract.md", staging / "next" / "tests" / "contract.md")
        evidence = staging / "evidence"
        evidence.mkdir()
        evidence_sha: dict[str, str] = {}
        for name in _EVIDENCE_FILES:
            source = candidate.root / name
            if not source.is_file():
                raise SkillActivationError(f"approved candidate evidence is missing: {name}")
            shutil.copy2(source, evidence / name)
            evidence_sha[name] = _file_sha(evidence / name)
        next_fingerprint = skill_tree_fingerprint(staging / "next")
        if next_fingerprint == observed:
            raise SkillActivationError("replacement skill is identical to the live skill")
        identity = {
            "candidate_id": candidate.candidate_id,
            "candidate_fingerprint": candidate.fingerprint,
            "skill_id": skill_id,
            "previous_fingerprint": observed,
            "next_fingerprint": next_fingerprint,
            "evidence_sha256": evidence_sha,
        }
        now = _now()
        manifest = {
            "schema_version": 1,
            **identity,
            "deployment_fingerprint": hashlib.sha256(_canonical(identity)).hexdigest(),
            "status": "staged",
            "staged_at": now,
            "staged_by": _text(actor, "actor"),
            "activated_at": "",
            "activated_by": "",
            "rolled_back_at": "",
            "rolled_back_by": "",
        }
        _atomic_json(staging / "activation.json", manifest)
        replace_with_retry(staging, root)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return load_skill_activation(root)


def load_skill_activation(root: Path) -> SkillActivation:
    supplied = Path(root)
    if not supplied.is_dir() or _linked(supplied):
        raise SkillActivationError("skill activation is not a real directory")
    root = supplied.resolve()
    manifest = _json(root / "activation.json", "activation manifest")
    if manifest.get("schema_version") != 1 or manifest.get("status") not in _STATES:
        raise SkillActivationError("skill activation state is invalid")
    identity = {
        key: manifest.get(key)
        for key in (
            "candidate_id", "candidate_fingerprint", "skill_id", "previous_fingerprint",
            "next_fingerprint", "evidence_sha256",
        )
    }
    if hashlib.sha256(_canonical(identity)).hexdigest() != manifest.get("deployment_fingerprint"):
        raise SkillActivationError("skill activation identity changed")
    if skill_tree_fingerprint(root / "previous") != manifest.get("previous_fingerprint"):
        raise SkillActivationError("skill activation previous snapshot changed")
    if skill_tree_fingerprint(root / "next") != manifest.get("next_fingerprint"):
        raise SkillActivationError("skill activation next snapshot changed")
    expected = manifest.get("evidence_sha256")
    if not isinstance(expected, dict) or set(expected) != set(_EVIDENCE_FILES):
        raise SkillActivationError("skill activation evidence set changed")
    for name, digest in expected.items():
        path = root / "evidence" / name
        if not path.is_file() or _linked(path) or _file_sha(path) != digest:
            raise SkillActivationError(f"skill activation evidence changed: {name}")
    return SkillActivation(root, manifest)


def activate_skill(
    deployment: SkillActivation,
    *,
    skill_library_root: Path,
    actor: str,
    expected_fingerprint: str,
) -> SkillActivation:
    """Switch to the evaluated tree; restore the old tree on a caught failure."""

    deployment = load_skill_activation(deployment.root)
    if deployment.fingerprint != str(expected_fingerprint or ""):
        raise SkillActivationError("skill activation changed; reload before activation")
    # The live Skill is shared by every project.  A deployment-scoped lock
    # would let two projects replace the same directory at the same time, so
    # the mutation lock is keyed by the Skill and lives beside the shared
    # library instead of under either project's evidence directory.
    with _skill_lock(skill_library_root, str(deployment.manifest["skill_id"])):
        deployment = recover_skill_activation(deployment, skill_library_root=skill_library_root)
        if deployment.status == "active":
            return deployment
        if deployment.status != "staged":
            raise SkillActivationError(f"skill activation cannot start in state {deployment.status}")
        live, incoming, displaced = _switch_paths(deployment, skill_library_root)
        if skill_tree_fingerprint(live) != deployment.manifest["previous_fingerprint"]:
            raise SkillActivationError("live skill drifted before activation")
        _set_state(deployment, "activating")
        shutil.copytree(deployment.root / "next", incoming)
        try:
            replace_with_retry(live, displaced)
        except OSError as exc:
            shutil.rmtree(incoming, ignore_errors=True)
            _set_state(load_skill_activation(deployment.root), "staged")
            raise SkillActivationError("skill activation did not start; the previous version remains active") from exc
        try:
            replace_with_retry(incoming, live)
        except OSError as exc:
            try:
                replace_with_retry(displaced, live)
            except OSError as restore_exc:
                raise SkillActivationError(
                    "skill activation was interrupted during restore; explicit recovery is required"
                ) from restore_exc
            shutil.rmtree(incoming, ignore_errors=True)
            _set_state(load_skill_activation(deployment.root), "staged")
            raise SkillActivationError("skill activation failed; the previous version was restored") from exc
        active = _set_state(
            load_skill_activation(deployment.root), "active",
            activated_at=_now(), activated_by=_text(actor, "actor"),
        )
        shutil.rmtree(displaced, ignore_errors=True)
        return active


def rollback_skill(
    deployment: SkillActivation,
    *,
    skill_library_root: Path,
    actor: str,
    expected_fingerprint: str,
) -> SkillActivation:
    """Restore the sealed old tree without consulting the candidate directory."""

    deployment = load_skill_activation(deployment.root)
    if deployment.fingerprint != str(expected_fingerprint or ""):
        raise SkillActivationError("skill activation changed; reload before rollback")
    with _skill_lock(skill_library_root, str(deployment.manifest["skill_id"])):
        deployment = recover_skill_activation(deployment, skill_library_root=skill_library_root)
        if deployment.status == "rolled_back":
            return deployment
        if deployment.status != "active":
            raise SkillActivationError(f"skill rollback cannot start in state {deployment.status}")
        live, incoming, displaced = _switch_paths(deployment, skill_library_root)
        if skill_tree_fingerprint(live) != deployment.manifest["next_fingerprint"]:
            raise SkillActivationError("active skill drifted before rollback")
        _set_state(deployment, "rolling_back")
        shutil.copytree(deployment.root / "previous", incoming)
        try:
            replace_with_retry(live, displaced)
        except OSError as exc:
            shutil.rmtree(incoming, ignore_errors=True)
            _set_state(load_skill_activation(deployment.root), "active")
            raise SkillActivationError("skill rollback did not start; the evaluated version remains active") from exc
        try:
            replace_with_retry(incoming, live)
        except OSError as exc:
            try:
                replace_with_retry(displaced, live)
            except OSError as restore_exc:
                raise SkillActivationError(
                    "skill rollback was interrupted during restore; explicit recovery is required"
                ) from restore_exc
            shutil.rmtree(incoming, ignore_errors=True)
            _set_state(load_skill_activation(deployment.root), "active")
            raise SkillActivationError("skill rollback failed; the active version was restored") from exc
        restored = _set_state(
            load_skill_activation(deployment.root), "rolled_back",
            rolled_back_at=_now(), rolled_back_by=_text(actor, "actor"),
        )
        shutil.rmtree(displaced, ignore_errors=True)
        return restored


def recover_skill_activation(
    deployment: SkillActivation,
    *,
    skill_library_root: Path,
) -> SkillActivation:
    """Resolve an interrupted switch from fingerprints, never from timestamps."""

    deployment = load_skill_activation(deployment.root)
    if deployment.status not in {"activating", "rolling_back"}:
        return deployment
    live, incoming, displaced = _switch_paths(deployment, skill_library_root)
    previous = deployment.manifest["previous_fingerprint"]
    next_fingerprint = deployment.manifest["next_fingerprint"]
    live_fingerprint = skill_tree_fingerprint(live) if live.is_dir() else ""
    if deployment.status == "activating":
        if live_fingerprint == next_fingerprint:
            _cleanup_switch_paths(incoming, displaced)
            return _set_state(deployment, "active")
        if live_fingerprint == previous:
            _cleanup_switch_paths(incoming, displaced)
            return _set_state(deployment, "staged")
        if not live.exists() and displaced.is_dir() and skill_tree_fingerprint(displaced) == previous:
            replace_with_retry(displaced, live)
            _cleanup_switch_paths(incoming, displaced)
            return _set_state(deployment, "staged")
    else:
        if live_fingerprint == previous:
            _cleanup_switch_paths(incoming, displaced)
            return _set_state(deployment, "rolled_back")
        if live_fingerprint == next_fingerprint:
            _cleanup_switch_paths(incoming, displaced)
            return _set_state(deployment, "active")
        if not live.exists() and displaced.is_dir() and skill_tree_fingerprint(displaced) == next_fingerprint:
            replace_with_retry(displaced, live)
            _cleanup_switch_paths(incoming, displaced)
            return _set_state(deployment, "active")
    raise SkillActivationError("interrupted skill switch has an unknown filesystem state")


def _switch_paths(deployment: SkillActivation, skill_library_root: Path) -> tuple[Path, Path, Path]:
    live = _child(Path(skill_library_root), str(deployment.manifest["skill_id"]), "live skill")
    token = deployment.fingerprint[:16]
    return live, live.parent / f".superones-incoming-{token}", live.parent / f".superones-displaced-{token}"


def _cleanup_switch_paths(incoming: Path, displaced: Path) -> None:
    for path in (incoming, displaced):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


@contextmanager
def _skill_lock(skill_library_root: Path, skill_id: str) -> Iterator[None]:
    library = Path(skill_library_root).resolve()
    library.mkdir(parents=True, exist_ok=True)
    # Keep coordination files outside the library enumeration boundary.  A
    # hidden directory under ``skills/`` would still look like a Skill to a
    # filesystem scanner that intentionally does not rely on dot semantics.
    library_key = hashlib.sha256(str(library).encode("utf-8")).hexdigest()[:16]
    lock_root = library.parent / ".superones-skill-activation-locks" / library_key
    lock_root.mkdir(parents=True, exist_ok=True)
    # Do not put an untrusted identifier in a filename.  The readable prefix
    # is only diagnostic; the digest is the identity and keeps the path flat.
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(skill_id))[:32] or "skill"
    digest = hashlib.sha256(str(skill_id).encode("utf-8")).hexdigest()[:24]
    path = lock_root / f"{safe}-{digest}.lock"
    with path.open("a+b") as stream:
        try:
            lock_file_descriptor(stream.fileno(), blocking=False)
        except OSError as exc:
            raise SkillActivationError("another process is changing this skill") from exc
        try:
            yield
        finally:
            try:
                unlock_file_descriptor(stream.fileno())
            except OSError:
                pass


def _set_state(deployment: SkillActivation, state: str, **fields: str) -> SkillActivation:
    manifest = dict(deployment.manifest)
    manifest.update({"status": state, **fields})
    _atomic_json(deployment.root / "activation.json", manifest)
    return load_skill_activation(deployment.root)


def _child(root: Path, name: str, label: str) -> Path:
    base = root.resolve()
    target = (base / str(name or "")).resolve()
    if not name or target.parent != base or _linked(target):
        raise SkillActivationError(f"{label} path is unsafe")
    return target


def _json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SkillActivationError(f"{label} is unreadable") from exc
    if not isinstance(payload, dict):
        raise SkillActivationError(f"{label} is invalid")
    return payload


def _linked(path: Path) -> bool:
    return path.exists() and (path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400))


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 128:
        raise SkillActivationError(f"{name} is required")
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
