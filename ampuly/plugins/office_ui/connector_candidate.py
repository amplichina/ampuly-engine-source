"""Generated connector candidates and their fail-closed admission lifecycle.

The generator deliberately emits a declarative adapter bundle instead of
arbitrary Python.  A model may propose a contract, but the product validates
that contract, derives every generated file from it, and binds review and
registration to the resulting content fingerprint.

Nothing in this module reads a credential, reaches a host, or enables a
connector.  The caller supplies the local contract-test client and an owner
must explicitly review a passing candidate before it can be registered.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from ampuly.core.config import replace_with_retry

from .connector_contract import ConnectorContract, from_payload, run_contract_tests, summarize


class ConnectorCandidateError(ValueError):
    """A candidate transition that cannot be proven safe."""


@dataclass(frozen=True)
class ConnectorCandidate:
    root: Path
    manifest: dict[str, Any]

    @property
    def candidate_id(self) -> str:
        return str(self.manifest.get("candidate_id", ""))

    @property
    def status(self) -> str:
        return str(self.manifest.get("status", ""))

    @property
    def fingerprint(self) -> str:
        return str(self.manifest.get("candidate_fingerprint", ""))

    @property
    def contract(self) -> ConnectorContract:
        return from_payload(_read_json(self.root / "contract.json"))


def create_candidate(
    root: Path,
    proposal: dict[str, Any],
    *,
    actor: str,
    conversation_id: str,
    turn_id: str,
) -> ConnectorCandidate:
    """Create an inert candidate bundle from one structured model proposal."""

    actor = _required(actor, "actor")
    conversation_id = _required(conversation_id, "conversation_id")
    turn_id = _required(turn_id, "turn_id")
    contract_payload = dict(proposal.get("contract", {}) or {})
    contract_payload["provenance"] = "generated"
    contract = from_payload(contract_payload)
    catalog_admission = _catalog_admission(proposal.get("catalog_admission"))
    if catalog_admission and _catalog_runtime_name(catalog_admission["catalog_id"]) != contract.name:
        raise ConnectorCandidateError("catalog id must match connector contract name")
    candidate_id = f"{contract.name}-{contract.fingerprint()[:16]}"
    base = Path(root).resolve()
    base.mkdir(parents=True, exist_ok=True)
    candidate_root = (base / candidate_id).resolve()
    if candidate_root.parent != base:
        raise ConnectorCandidateError("candidate path escaped its store")
    if candidate_root.exists():
        existing = load_candidate(candidate_root)
        if existing.fingerprint != contract.fingerprint():
            raise ConnectorCandidateError("candidate id collision")
        existing_admission = dict(dict(existing.manifest.get("provenance", {}) or {}).get("catalog_admission", {}) or {})
        if existing_admission != catalog_admission:
            raise ConnectorCandidateError("candidate catalog admission differs from the existing immutable candidate")
        return existing

    source_text = json.dumps(proposal, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    created_at = _now()
    provenance = {
        "kind": "conversation",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "proposal_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "source": contract.source,
    }
    if catalog_admission:
        provenance["catalog_admission"] = catalog_admission
    manifest = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "connector": contract.name,
        "version": contract.version,
        "status": "draft",
        "candidate_fingerprint": contract.fingerprint(),
        "created_at": created_at,
        "created_by": actor,
        "provenance": provenance,
        "generated_files": [
            "contract.json", "config.schema.json", "adapter.json", "provenance.json"
        ],
    }
    files = {
        "contract.json": contract.to_payload(),
        "config.schema.json": _config_schema(contract),
        "adapter.json": _adapter_manifest(contract),
        "provenance.json": manifest["provenance"],
        "candidate.json": manifest,
    }
    if catalog_admission.get("route") == "generate_declarative_candidate":
        from .connector_scaffold import scaffold_files

        generated = scaffold_files(contract)
        files.update(generated)
        manifest["generated_files"].extend(generated)
        files["candidate.json"] = manifest
    _write_new_bundle(candidate_root, files)
    return ConnectorCandidate(candidate_root, manifest)


def run_candidate_tests(
    candidate: ConnectorCandidate,
    *,
    base_url: str,
    client: Callable[..., tuple[int, str]],
    credential: str = "",
    write_authorized: bool = False,
) -> ConnectorCandidate:
    """Run the contract checks and preserve their exact evidence."""

    _assert_current(candidate)
    if candidate.status not in {"draft", "test_failed", "tested"}:
        raise ConnectorCandidateError(
            "reviewed or registered candidates are immutable; generate a new candidate to change evidence"
        )
    results = run_contract_tests(
        candidate.contract,
        base_url=base_url,
        client=client,
        credential=credential,
        write_authorized=write_authorized,
    )
    report = summarize(results)
    evidence = {
        "schema_version": 1,
        "candidate_fingerprint": candidate.fingerprint,
        "tested_at": _now(),
        "mode": "injected_local_sample",
        "write_authorized": bool(write_authorized),
        "summary": report,
        "results": [
            {"op_id": item.op_id, "check": item.check, "passed": item.passed, "detail": item.detail}
            for item in results
        ],
    }
    _atomic_json(candidate.root / "test-evidence.json", evidence)
    manifest = dict(candidate.manifest)
    manifest["status"] = "tested" if report["ok"] else "test_failed"
    manifest["test_evidence_sha256"] = _file_sha256(candidate.root / "test-evidence.json")
    manifest["tested_at"] = evidence["tested_at"]
    _atomic_json(candidate.root / "candidate.json", manifest)
    return ConnectorCandidate(candidate.root, manifest)


def review_candidate(
    candidate: ConnectorCandidate,
    *,
    actor: str,
    approved: bool,
    expected_fingerprint: str,
    note: str,
) -> ConnectorCandidate:
    """Record an owner's rejectable review, bound to the reviewed bytes."""

    actor = _required(actor, "actor")
    note = _required(note, "note")
    _assert_expected(candidate, expected_fingerprint)
    _assert_current(candidate)
    if candidate.status != "tested":
        raise ConnectorCandidateError("only a passing tested candidate can be reviewed")
    evidence = _read_json(candidate.root / "test-evidence.json")
    if not bool(dict(evidence.get("summary", {}) or {}).get("ok")):
        raise ConnectorCandidateError("test evidence is not passing")
    review = {
        "candidate_fingerprint": candidate.fingerprint,
        "approved": bool(approved),
        "actor": actor,
        "note": note,
        "reviewed_at": _now(),
    }
    _atomic_json(candidate.root / "review.json", review)
    manifest = dict(candidate.manifest)
    manifest["status"] = "approved" if approved else "rejected"
    manifest["review_sha256"] = _file_sha256(candidate.root / "review.json")
    manifest["reviewed_at"] = review["reviewed_at"]
    _atomic_json(candidate.root / "candidate.json", manifest)
    return ConnectorCandidate(candidate.root, manifest)


def register_candidate(
    candidate: ConnectorCandidate,
    registry_root: Path,
    *,
    actor: str,
    expected_fingerprint: str,
    health_client: Callable[..., tuple[int, str]],
    base_url: str,
    credential: str = "",
) -> dict[str, Any]:
    """Register one approved version after a read-only health check.

    Registration is a version lock, not activation.  The existing connector
    switch remains the only activation boundary.
    """

    actor = _required(actor, "actor")
    _assert_expected(candidate, expected_fingerprint)
    _assert_current(candidate)
    if candidate.status not in {"approved", "registered"}:
        raise ConnectorCandidateError("candidate must be approved before registration")
    contract = candidate.contract
    registry = Path(registry_root).resolve()
    registry.mkdir(parents=True, exist_ok=True)
    target = (registry / f"{contract.name}.json").resolve()
    if target.parent != registry:
        raise ConnectorCandidateError("registry path escaped its store")
    if target.exists():
        existing = _read_json(target)
        if str(existing.get("candidate_fingerprint", "")) != candidate.fingerprint:
            raise ConnectorCandidateError(
                "connector is locked to another fingerprint; revoke it before registering a replacement"
            )
        if existing.get("status") == "revoked":
            raise ConnectorCandidateError(
                "connector registration is revoked; create a newly reviewed version before registering"
            )
        # A lost reply after the atomic registry write is a retry, not a new
        # registration. Bring the candidate marker up to date and return the
        # authoritative entry without probing or overwriting it again.
        manifest = dict(candidate.manifest)
        manifest["status"] = "registered"
        manifest["registered_at"] = str(existing.get("registered_at", ""))
        _atomic_json(candidate.root / "candidate.json", manifest)
        return existing
    if not contract.reads:
        raise ConnectorCandidateError("registration health requires at least one read operation")
    read_only = ConnectorContract(
        name=contract.name,
        version=contract.version,
        base_url_env=contract.base_url_env,
        auth_type=contract.auth_type,
        credential_env=contract.credential_env,
        operations=(contract.reads[0],),
        source=contract.source,
        provenance=contract.provenance,
    )
    health = summarize(run_contract_tests(
        read_only,
        base_url=base_url,
        client=health_client,
        credential=credential,
        write_authorized=False,
    ))
    if not health["ok"]:
        raise ConnectorCandidateError("candidate health check failed")
    entry = {
        "schema_version": 1,
        "connector": contract.name,
        "version": contract.version,
        "candidate_id": candidate.candidate_id,
        "candidate_fingerprint": candidate.fingerprint,
        "adapter_runtime": "ampuly.http.declarative.v1",
        "capabilities": {
            "read": [op.op_id for op in contract.reads],
            "write": [op.op_id for op in contract.writes],
        },
        "credential_env": contract.credential_env,
        "base_url_env": contract.base_url_env,
        "registered_at": _now(),
        "registered_by": actor,
        "health_mode": "injected_local_sample",
        "health": health,
        "status": "registered",
        "enabled": False,
    }
    catalog_admission = dict(dict(candidate.manifest.get("provenance", {}) or {}).get("catalog_admission", {}) or {})
    if catalog_admission:
        entry["catalog_admission"] = catalog_admission
    _atomic_json(target, entry)
    manifest = dict(candidate.manifest)
    manifest["status"] = "registered"
    manifest["registered_at"] = entry["registered_at"]
    _atomic_json(candidate.root / "candidate.json", manifest)
    return entry


def revoke_registration(
    registry_root: Path,
    connector: str,
    *,
    actor: str,
    expected_fingerprint: str,
    reason: str,
) -> dict[str, Any]:
    """Disable a registered connector while retaining its audit record."""

    actor = _required(actor, "actor")
    reason = _required(reason, "reason")
    name = str(connector or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name):
        raise ConnectorCandidateError("connector name is invalid")
    registry = Path(registry_root).resolve()
    target = (registry / f"{name}.json").resolve()
    if target.parent != registry or not target.is_file():
        raise ConnectorCandidateError("connector is not registered")
    entry = _read_json(target)
    if str(entry.get("candidate_fingerprint", "")) != str(expected_fingerprint or "").strip():
        raise ConnectorCandidateError("registered fingerprint changed; reload before revoking")
    if entry.get("status") == "revoked":
        return entry
    updated = dict(entry)
    updated.update({
        "enabled": False,
        "status": "revoked",
        "revoked_at": _now(),
        "revoked_by": actor,
        "revocation_reason": reason,
    })
    _atomic_json(target, updated)
    return updated


def load_candidate(root: Path) -> ConnectorCandidate:
    root = Path(root).resolve()
    manifest = _read_json(root / "candidate.json")
    candidate = ConnectorCandidate(root, manifest)
    _assert_current(candidate)
    return candidate


def candidate_by_id(root: Path, candidate_id: str) -> ConnectorCandidate:
    """Resolve one candidate without allowing an id to escape its store."""

    candidate_id = str(candidate_id or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}-[a-f0-9]{16}", candidate_id):
        raise ConnectorCandidateError("candidate id is invalid")
    base = Path(root).resolve()
    target = (base / candidate_id).resolve()
    if target.parent != base or not target.is_dir() or target.is_symlink():
        raise ConnectorCandidateError("candidate does not exist")
    return load_candidate(target)


def project_candidate_root(ampuly_home: Path, project_id: str) -> Path:
    """A project-scoped store without putting an untrusted id in a path."""

    project = _required(project_id, "project_id")
    key = hashlib.sha256(project.encode("utf-8")).hexdigest()[:24]
    return Path(ampuly_home).resolve() / "connector_candidates" / "projects" / key


def list_candidates(root: Path) -> list[dict[str, Any]]:
    """List bounded, non-secret candidate metadata for one project."""

    base = Path(root).resolve()
    if not base.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(base.iterdir(), key=lambda item: item.name)[:500]:
        if not path.is_dir() or path.is_symlink():
            continue
        try:
            candidate = load_candidate(path)
        except ConnectorCandidateError:
            continue
        rows.append({
            "candidate_id": candidate.candidate_id,
            "connector": str(candidate.manifest.get("connector", "")),
            "version": str(candidate.manifest.get("version", "")),
            "status": candidate.status,
            "candidate_fingerprint": candidate.fingerprint,
            "created_at": str(candidate.manifest.get("created_at", "")),
            "catalog_admission": dict(dict(candidate.manifest.get("provenance", {}) or {}).get("catalog_admission", {}) or {}),
        })
    return rows


def list_registrations(registry_root: Path) -> list[dict[str, Any]]:
    """List bounded registry metadata without exposing environment values."""

    registry = Path(registry_root).resolve()
    if not registry.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(registry.glob("*.json"), key=lambda item: item.name)[:500]:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            entry = _read_json(path)
        except ConnectorCandidateError:
            continue
        rows.append({
            "connector": str(entry.get("connector", "")),
            "version": str(entry.get("version", "")),
            "candidate_id": str(entry.get("candidate_id", "")),
            "candidate_fingerprint": str(entry.get("candidate_fingerprint", "")),
            "status": str(entry.get("status", "registered")),
            "enabled": bool(entry.get("enabled", False)),
            "registered_at": str(entry.get("registered_at", "")),
            "catalog_admission": dict(entry.get("catalog_admission", {}) or {}),
        })
    return rows


def registration_by_name(registry_root: Path, connector: str) -> dict[str, Any]:
    """Read authoritative, non-secret registration metadata by safe connector name."""

    name = str(connector or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name):
        raise ConnectorCandidateError("connector name is invalid")
    registry = Path(registry_root).resolve()
    target = (registry / f"{name}.json").resolve()
    if target.parent != registry or not target.is_file() or target.is_symlink():
        raise ConnectorCandidateError("connector is not registered")
    entry = _read_json(target)
    if str(entry.get("connector", "")) != name:
        raise ConnectorCandidateError("connector registration identity is invalid")
    return {
        "connector": name,
        "version": str(entry.get("version", "")),
        "candidate_id": str(entry.get("candidate_id", "")),
        "candidate_fingerprint": str(entry.get("candidate_fingerprint", "")),
        "status": str(entry.get("status", "registered")),
        "enabled": bool(entry.get("enabled", False)),
        "registered_at": str(entry.get("registered_at", "")),
        "catalog_admission": dict(entry.get("catalog_admission", {}) or {}),
    }


def _catalog_admission(value: Any) -> dict[str, str]:
    """Keep only the non-secret catalog identity that governance can verify."""

    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ConnectorCandidateError("catalog admission must be an object")
    catalog_id = str(value.get("catalog_id", "") or "").strip()
    fingerprint = str(value.get("catalog_fingerprint", "") or "").strip().lower()
    route = str(value.get("route", "") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", catalog_id):
        raise ConnectorCandidateError("catalog admission id is invalid")
    if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise ConnectorCandidateError("catalog admission fingerprint is invalid")
    if route not in {
        "inspect_existing_mcp", "generate_declarative_candidate",
        "computer_use_candidate", "vendor_connector_review",
    }:
        raise ConnectorCandidateError("catalog admission route is invalid")
    return {"catalog_id": catalog_id, "catalog_fingerprint": fingerprint, "route": route}


def _catalog_runtime_name(catalog_id: str) -> str:
    return re.sub(r"[.-]+", "_", str(catalog_id or ""))


def _adapter_manifest(contract: ConnectorContract) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runtime": "ampuly.http.declarative.v1",
        "contract_fingerprint": contract.fingerprint(),
        "base_url_from": {"kind": "environment", "name": contract.base_url_env},
        "credential_from": (
            {"kind": "none"} if contract.auth_type == "none"
            else {"kind": "environment", "name": contract.credential_env}
        ),
        "operations": [
            {
                "tool_name": f"connector.{contract.name}.{op.op_id}",
                "op_id": op.op_id,
                "effect": op.kind,
                "approval_required": op.kind == "write",
                "unknown_outcome": op.unknown_outcome,
            }
            for op in contract.operations
        ],
    }


def _config_schema(contract: ConnectorContract) -> dict[str, Any]:
    required = [contract.base_url_env]
    properties: dict[str, Any] = {
        contract.base_url_env: {"type": "string", "format": "uri", "writeOnly": True}
    }
    if contract.auth_type != "none":
        required.append(contract.credential_env)
        properties[contract.credential_env] = {"type": "string", "writeOnly": True}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _assert_current(candidate: ConnectorCandidate) -> None:
    actual = candidate.contract.fingerprint()
    if actual != candidate.fingerprint:
        raise ConnectorCandidateError("candidate content drifted; review and admission are invalid")
    for name in list(candidate.manifest.get("generated_files", []) or []):
        path = (candidate.root / str(name)).resolve()
        if path.parent != candidate.root.resolve() or not path.is_file():
            raise ConnectorCandidateError(f"generated file missing or out of scope: {name}")


def _assert_expected(candidate: ConnectorCandidate, expected: str) -> None:
    if not str(expected or "").strip() or str(expected).strip() != candidate.fingerprint:
        raise ConnectorCandidateError("candidate fingerprint changed; reload before deciding")


def _write_new_bundle(root: Path, files: dict[str, Any]) -> None:
    parent = root.parent
    staging = Path(tempfile.mkdtemp(prefix=f".{root.name}.", dir=str(parent)))
    try:
        for name, payload in files.items():
            target = staging / name
            if isinstance(payload, dict):
                _atomic_json(target, payload)
            else:
                target.write_text(str(payload), encoding="utf-8", newline="\n")
        replace_with_retry(staging, root)
    except Exception:
        for child in (staging.iterdir() if staging.exists() else ()):
            if child.is_file():
                child.unlink()
        if staging.exists():
            staging.rmdir()
        raise


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectorCandidateError(f"cannot read candidate file {path.name}") from exc
    if not isinstance(payload, dict):
        raise ConnectorCandidateError(f"candidate file {path.name} must contain an object")
    return payload


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _required(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ConnectorCandidateError(f"{field} is required")
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
