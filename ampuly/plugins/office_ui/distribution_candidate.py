"""Cross-customer distribution candidates: a signed, desensitized copy of one shareable workflow version.

The center never receives a workflow because a customer accepted it.  A candidate is exported only when the
asset projection says the exact version is *shareable* (qualified, and explicitly authorized by a customer on
their accepted order); it carries the package's own files, no customer data, no trial notes and no inline
credentials; and it is signed with a key that lives only in the environment.  The receiving side verifies the
signature and every byte, records the owner's decision, and lands an accepted candidate as a *draft* that must
walk the ordinary release gates -- it never overwrites a local customization and never becomes active by itself.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import yaml

from .release_runtime_evidence import build_runtime_evidence
from .workflow_asset import project_workflow_asset
from .workflow_packages import WorkflowPackage, draft_manifest_from, load_package
from ampuly.core.config import replace_with_retry

_KIND = "superones.distribution-candidate"
_SCHEMA_VERSION = 1
SIGNING_KEY_ENV = "SUPERONES_DISTRIBUTION_SIGNING_KEY"
_MIN_KEY_CHARS = 32
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_EXCLUDED_TOP_LEVEL = {"tests", ".releases"}
# What must never leave a customer's machine inside a candidate: a value that looks like a credential. An
# environment *reference* (``api_key_env: NAME``) is fine; a literal secret is not.
_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|authorization)\b(?!_env)\s*[:=]\s*[\"']?[A-Za-z0-9_\-./+=]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class DistributionCandidateError(ValueError):
    """A candidate that must not be exported, trusted or landed."""


def signing_key_from_env(environ: Mapping[str, str] | None = None) -> bytes:
    """The signing key, from the environment only; never stored, logged or echoed."""
    value = str((environ if environ is not None else os.environ).get(SIGNING_KEY_ENV, "") or "").strip()
    if len(value) < _MIN_KEY_CHARS:
        raise DistributionCandidateError(f"distribution signing key is not configured; set {SIGNING_KEY_ENV} (at least {_MIN_KEY_CHARS} characters)")
    return value.encode("utf-8")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def _sign(key: bytes, bundle_digest: str) -> dict[str, str]:
    return {"alg": "hmac-sha256", "key_id": _key_id(key), "value": hmac.new(key, bundle_digest.encode("utf-8"), hashlib.sha256).hexdigest()}


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def inline_credential_in(text: str) -> str:
    """The first credential-looking line in a text, or an empty string."""
    for line_no, line in enumerate(text.splitlines(), start=1):
        if any(pattern.search(line) for pattern in _CREDENTIAL_PATTERNS):
            return f"line {line_no}"
    return ""


def _safe_member(member: str) -> bool:
    relative = PurePosixPath(member)
    return (
        bool(member) and not relative.is_absolute() and "\\" not in member and ":" not in member
        and not any(part in {"", ".", ".."} for part in relative.parts)
        and (member.startswith("package/") or member.startswith("skills/"))
    )


def _collect_files(package: WorkflowPackage) -> list[tuple[str, Path]]:
    """(member, path) for the package's own files and the skills it locks; nothing customer-scoped."""
    root = package.root.resolve()
    members: list[tuple[str, Path]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise DistributionCandidateError(f"linked file inside the package is not exported: {path.name}")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts[0] in _EXCLUDED_TOP_LEVEL or relative.parts[0].startswith("."):
            continue
        members.append((f"package/{relative.as_posix()}", path))
    skills_root = package.skills_root.resolve()
    for skill_id in sorted(str(item) for item in list(package.skills or [])):
        if not _SAFE_ID.fullmatch(skill_id):
            raise DistributionCandidateError(f"skill id is not portable: {skill_id!r}")
        skill_dir = (skills_root / skill_id).resolve()
        try:
            skill_dir.relative_to(skills_root)
        except ValueError as exc:
            raise DistributionCandidateError(f"skill escapes skills_root: {skill_id}") from exc
        if not skill_dir.is_dir():
            raise DistributionCandidateError(f"locked skill is missing: {skill_id}")
        for path in sorted(skill_dir.rglob("*")):
            if path.is_symlink():
                raise DistributionCandidateError(f"linked file inside skill {skill_id} is not exported")
            if path.is_file():
                members.append((f"skills/{skill_id}/{path.relative_to(skill_dir).as_posix()}", path))
    return members


def _opaque(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def export_distribution_candidate(
    package: WorkflowPackage,
    business: Any,
    *,
    destination_dir: Path,
    actor: str,
    signing_key: bytes,
) -> dict[str, Any]:
    """Export one shareable published version as a signed candidate; refuse everything else."""
    clean_actor = str(actor or "").strip()
    if not clean_actor:
        raise DistributionCandidateError("export actor is required")
    if not _SAFE_ID.fullmatch(str(package.id or "")):
        raise DistributionCandidateError("workflow id is not portable")
    if package.status != "published":
        raise DistributionCandidateError(f"workflow {package.id!r} is {package.status}, not published")
    asset = project_workflow_asset(package, business)
    if not asset.shareable:
        raise DistributionCandidateError(f"workflow version is not shareable: {asset.sharing_blocker}")
    published = dict(build_runtime_evidence(package, business).get("published_evidence", {}) or {})
    fingerprint = str(published.get("candidate_fingerprint", "") or "").lower()
    manifest_sha = str(published.get("manifest_sha256", "") or "").lower()
    if not _SHA256.fullmatch(fingerprint) or not _SHA256.fullmatch(manifest_sha):
        raise DistributionCandidateError("published identity is incomplete")
    files: list[dict[str, Any]] = []
    payloads: list[tuple[str, bytes]] = []
    for member, path in _collect_files(package):
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        where = inline_credential_in(text) if text else ""
        if where:
            raise DistributionCandidateError(f"inline credential-like value in {member} ({where}); move it to an environment reference first")
        files.append({"member": member, "size": len(data), "sha256": _digest_bytes(data)})
        payloads.append((member, data))
    manifest: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "workflow_id": str(package.id),
        "version": str(package.manifest.get("version", "") or ""),
        "candidate_fingerprint": fingerprint,
        "manifest_sha256": manifest_sha,
        "asset": {
            "level": asset.level,
            "authorized_orders": asset.authorized_orders,
            "accepted_orders": asset.accepted_orders,
            "distinct_customers": asset.distinct_customers,
            "evidence_digest": asset.evidence_digest,
        },
        "provenance": {
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "exported_by": clean_actor,
            "source_ref": _opaque(str(package.root)),
        },
        "files": files,
        "total_bytes": sum(int(item["size"]) for item in files),
    }
    manifest["bundle_digest"] = _digest_bytes(_canonical(manifest))
    signature = _sign(signing_key, manifest["bundle_digest"])
    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"{package.id}-{manifest['version'] or 'unversioned'}-{fingerprint[:12]}.zip"
    if target.exists():
        existing = verify_distribution_candidate(target, signing_key=signing_key)
        # The same version exported again is the same candidate. When and by whom it was exported is not part of what
        # it is -- and neither is the bundle digest, which is computed over the provenance too: comparing it made a
        # second export of the same version "not match" whenever the clock had moved on by a second, which outside a
        # test is always.
        same = lambda payload: {k: v for k, v in payload.items() if k not in {"provenance", "bundle_digest"}}  # noqa: E731
        if same(existing) != same(manifest):
            raise DistributionCandidateError("an existing candidate at this path does not match this version")
        return {"path": str(target), "sha256": _digest_bytes(target.read_bytes()), "manifest": existing, "reused": True}
    temporary = target.with_name(target.name + ".part")
    with zipfile.ZipFile(temporary, "w") as archive:
        archive.writestr(_zip_info("candidate.json"), _canonical(manifest))
        archive.writestr(_zip_info("signature.json"), _canonical(signature))
        for member, data in payloads:
            archive.writestr(_zip_info(member), data)
    replace_with_retry(temporary, target)
    return {"path": str(target), "sha256": _digest_bytes(target.read_bytes()), "manifest": manifest, "reused": False}


def verify_distribution_candidate(path: str | Path, *, signing_key: bytes) -> dict[str, Any]:
    """Verify the signature, the member set and every byte without extracting anything."""
    source = Path(path)
    try:
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            if len(names) < 2 or names[:2] != ["candidate.json", "signature.json"] or len(names) != len(set(names)):
                raise DistributionCandidateError("candidate member list is invalid")
            try:
                manifest = json.loads(archive.read("candidate.json").decode("utf-8"))
                signature = json.loads(archive.read("signature.json").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DistributionCandidateError("candidate manifest is invalid") from exc
            if not isinstance(manifest, dict) or not isinstance(signature, dict):
                raise DistributionCandidateError("candidate manifest must be an object")
            if manifest.get("kind") != _KIND or int(manifest.get("schema_version", 0) or 0) != _SCHEMA_VERSION:
                raise DistributionCandidateError("candidate contract is unsupported")
            expected_digest = str(manifest.get("bundle_digest", "") or "")
            body = {key: value for key, value in manifest.items() if key != "bundle_digest"}
            if _digest_bytes(_canonical(body)) != expected_digest:
                raise DistributionCandidateError("candidate manifest digest mismatch")
            wanted = _sign(signing_key, expected_digest)
            if signature.get("alg") != wanted["alg"] or not hmac.compare_digest(str(signature.get("value", "") or ""), wanted["value"]):
                raise DistributionCandidateError("candidate signature does not verify with the configured key")
            if not _SAFE_ID.fullmatch(str(manifest.get("workflow_id", "") or "")):
                raise DistributionCandidateError("candidate workflow id is not portable")
            files = list(manifest.get("files", []) or [])
            if names != ["candidate.json", "signature.json", *[str(item.get("member", "")) for item in files]]:
                raise DistributionCandidateError("candidate member list does not match its manifest")
            if not any(str(item.get("member", "")) == "package/workflow.yaml" for item in files):
                raise DistributionCandidateError("candidate carries no workflow manifest")
            total = 0
            for item in files:
                member = str(item.get("member", "") or "")
                if not _safe_member(member):
                    raise DistributionCandidateError(f"candidate member is unsafe: {member}")
                info = archive.getinfo(member)
                size = int(item.get("size", -1))
                if info.file_size != size:
                    raise DistributionCandidateError(f"candidate member size mismatch: {member}")
                if _digest_bytes(archive.read(member)) != str(item.get("sha256", "") or ""):
                    raise DistributionCandidateError(f"candidate member digest mismatch: {member}")
                total += size
            if total != int(manifest.get("total_bytes", -1)):
                raise DistributionCandidateError("candidate total byte count mismatch")
            return manifest
    except (OSError, zipfile.BadZipFile) as exc:
        raise DistributionCandidateError("candidate is not a valid ZIP archive") from exc


def _inbox_record_path(ampuly_home: Path, manifest: dict[str, Any]) -> Path:
    return Path(ampuly_home) / "distribution-inbox" / str(manifest["workflow_id"]) / f"{manifest['candidate_fingerprint']}.json"


def distribution_inbox(ampuly_home: Path) -> list[dict[str, Any]]:
    """Every candidate this installation has verified, with the owner's decision on it."""
    root = Path(ampuly_home) / "distribution-inbox"
    rows: list[dict[str, Any]] = []
    if not root.is_dir():
        return rows
    for path in sorted(root.rglob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def receive_distribution_candidate(
    path: str | Path,
    *,
    ampuly_home: Path,
    actor: str,
    decision: str,
    signing_key: bytes,
    note: str = "",
) -> dict[str, Any]:
    """Verify a candidate and record the owner's decision; an accepted one lands as a draft, nothing more."""
    clean_actor = str(actor or "").strip()
    if not clean_actor:
        raise DistributionCandidateError("review actor is required")
    clean_decision = str(decision or "").strip().lower()
    if clean_decision not in {"accept", "reject"}:
        raise DistributionCandidateError("decision must be accept or reject")
    source = Path(path)
    manifest = verify_distribution_candidate(source, signing_key=signing_key)
    home = Path(ampuly_home)
    record_path = _inbox_record_path(home, manifest)
    if record_path.exists():
        try:
            existing = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DistributionCandidateError("inbox record is unreadable") from exc
        if isinstance(existing, dict) and existing.get("status") in {"accepted", "rejected"}:
            return existing  # a decision is taken once
    record: dict[str, Any] = {
        "workflow_id": manifest["workflow_id"],
        "version": manifest["version"],
        "candidate_fingerprint": manifest["candidate_fingerprint"],
        "manifest_sha256": manifest["manifest_sha256"],
        "bundle_digest": manifest["bundle_digest"],
        "bundle_sha256": _digest_bytes(source.read_bytes()),
        "asset": dict(manifest.get("asset", {}) or {}),
        "status": "rejected" if clean_decision == "reject" else "accepted",
        "decided_by": clean_actor,
        "decided_at": datetime.now().isoformat(timespec="seconds"),
        "note": str(note or "").strip()[:500],
        "draft_id": "",
    }
    if clean_decision == "accept":
        record["draft_id"] = _land_as_draft(source, manifest, home)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = record_path.with_name(record_path.name + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    replace_with_retry(temporary, record_path)
    return record


def _land_as_draft(source: Path, manifest: dict[str, Any], home: Path) -> str:
    """Extract the package as a draft under this home; refuse to touch anything that differs locally."""
    draft_id = f"{manifest['workflow_id']}-candidate-{manifest['candidate_fingerprint'][:8]}"
    draft_root = home / "workflows" / draft_id
    skills_root = home / "skills"
    with zipfile.ZipFile(source) as archive:
        members = [str(item.get("member", "")) for item in list(manifest.get("files", []) or [])]
        planned: list[tuple[Path, bytes]] = []
        for member in members:
            data = archive.read(member)
            if member.startswith("package/"):
                target = draft_root / PurePosixPath(member).relative_to("package")
            else:
                target = skills_root / PurePosixPath(member).relative_to("skills")
            resolved = target.resolve()
            if draft_root.resolve() not in resolved.parents and skills_root.resolve() not in resolved.parents:
                raise DistributionCandidateError(f"candidate member would land outside this home: {member}")
            if target.exists() and target.read_bytes() != data:
                raise DistributionCandidateError(f"local file differs from the candidate and is not overwritten: {member}")
            planned.append((target, data))
    if draft_root.exists() and (draft_root / "workflow.yaml").is_file():
        existing = load_package(draft_root)
        provenance = dict(existing.manifest.get("provenance", {}) or {}) if existing else {}
        if not existing or dict(provenance.get("distribution_candidate", {}) or {}).get("candidate_fingerprint") != manifest["candidate_fingerprint"]:
            raise DistributionCandidateError("a different draft already uses this candidate id")
        return draft_id
    for target, data in planned:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
    manifest_path = draft_root / "workflow.yaml"
    try:
        landed = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise DistributionCandidateError("candidate workflow manifest is not readable") from exc
    if not isinstance(landed, dict):
        raise DistributionCandidateError("candidate workflow manifest must be a mapping")
    # A landed candidate is a draft in this installation: it has not been trialled, approved or published here, and
    # the tool grants the sending side gave it are not grants this owner gave (draft_manifest_from, 4.7 Q01).
    landed = draft_manifest_from(landed)
    # The sending side's project, and the flow it was derived from there, name nothing in this installation; kept,
    # they would hide the draft from every project here but a same-named one (usable_by_project). It lands global.
    landed.pop("project_id", None)
    landed.pop("derived_from", None)
    landed["id"] = draft_id
    landed["status"] = "draft"
    provenance = dict(landed.get("provenance", {}) or {})
    provenance["distribution_candidate"] = {
        "workflow_id": manifest["workflow_id"],
        "version": manifest["version"],
        "candidate_fingerprint": manifest["candidate_fingerprint"],
        "manifest_sha256": manifest["manifest_sha256"],
        "bundle_digest": manifest["bundle_digest"],
    }
    landed["provenance"] = provenance
    manifest_path.write_text(yaml.safe_dump(landed, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return draft_id
