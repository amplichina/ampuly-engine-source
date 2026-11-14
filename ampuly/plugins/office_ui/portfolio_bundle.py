"""Customer-scoped, independently verifiable delivery evidence bundles.

An internal project backup is deliberately complete and therefore not a
customer hand-off format.  A portfolio bundle takes the opposite approach:
it contains only the final, already-verified acceptance snapshot and a small
allow-listed proof of the decision.  Customer identifiers, credentials,
absolute paths, prompts, memory and free-form event text never enter it.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from ampuly.plugins.office_ui.artifact_versions import _zip_info, verify_delivery_snapshot
from ampuly.plugins.office_ui.artifacts_service import workspace_root_for_project
from ampuly.core.config import replace_with_retry

_SCHEMA_VERSION = 1
_KIND = "superones.portfolio"
_SAFE_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,10}$")
_EXPORT_LOCK_GUARD = threading.Lock()
_EXPORT_LOCKS: dict[str, tuple[threading.Lock, int]] = {}


class PortfolioBundleError(ValueError):
    """A requested or received portfolio bundle cannot be trusted."""


@contextmanager
def _target_export_lock(path: Path):
    """Serialize only writers of the same content-addressed bundle."""

    key = str(path)
    with _EXPORT_LOCK_GUARD:
        lock, users = _EXPORT_LOCKS.get(key, (threading.Lock(), 0))
        _EXPORT_LOCKS[key] = (lock, users + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _EXPORT_LOCK_GUARD:
            current, users = _EXPORT_LOCKS[key]
            if users <= 1:
                _EXPORT_LOCKS.pop(key, None)
            else:
                _EXPORT_LOCKS[key] = (current, users - 1)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_stream(stream: Any) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return _digest_stream(stream)


def _opaque_ref(value: str) -> str:
    return _digest_bytes(str(value or "").encode("utf-8"))[:20]


def _artifact_member(index: int, source_path: str) -> str:
    suffix = PurePosixPath(str(source_path or "").replace("\\", "/")).suffix.lower()
    extension = suffix if _SAFE_EXTENSION.fullmatch(suffix) else ""
    return f"artifacts/artifact-{index:03d}{extension}"


def verify_portfolio_bundle(path: str | Path) -> dict[str, Any]:
    """Verify the exact member set and every byte without extracting it."""

    source = Path(path)
    try:
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            if not names or names[0] != "manifest.json" or len(names) != len(set(names)):
                raise PortfolioBundleError("portfolio member list is invalid")
            try:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PortfolioBundleError("portfolio manifest is invalid") from exc
            if not isinstance(manifest, dict):
                raise PortfolioBundleError("portfolio manifest must be an object")
            if manifest.get("kind") != _KIND or int(manifest.get("schema_version", 0) or 0) != _SCHEMA_VERSION:
                raise PortfolioBundleError("portfolio contract is unsupported")
            expected_digest = str(manifest.get("bundle_digest", "") or "")
            body = {key: value for key, value in manifest.items() if key != "bundle_digest"}
            if _digest_bytes(_canonical(body)) != expected_digest:
                raise PortfolioBundleError("portfolio manifest digest mismatch")
            files = list(manifest.get("files", []) or [])
            expected_names = ["manifest.json", *[str(item.get("member", "")) for item in files]]
            if names != expected_names:
                raise PortfolioBundleError("portfolio member list does not match its manifest")
            total = 0
            for item in files:
                member = str(item.get("member", "") or "")
                relative = PurePosixPath(member)
                if (
                    not member.startswith("artifacts/artifact-")
                    or relative.is_absolute()
                    or any(part in {"", ".", ".."} for part in relative.parts)
                ):
                    raise PortfolioBundleError("portfolio artifact member is unsafe")
                info = archive.getinfo(member)
                size = int(item.get("size", -1))
                if info.file_size != size:
                    raise PortfolioBundleError(f"portfolio artifact size mismatch: {member}")
                with archive.open(info) as stream:
                    if _digest_stream(stream) != str(item.get("sha256", "") or ""):
                        raise PortfolioBundleError(f"portfolio artifact digest mismatch: {member}")
                total += size
            if total != int(manifest.get("total_bytes", -1)):
                raise PortfolioBundleError("portfolio total byte count mismatch")
            return manifest
    except (OSError, zipfile.BadZipFile) as exc:
        raise PortfolioBundleError("portfolio is not a valid ZIP archive") from exc


def export_portfolio_bundle(
    engine: Any,
    business: Any,
    *,
    project_id: str,
    order_id: str,
) -> dict[str, Any]:
    """Export the final accepted artifact snapshot for one scoped order."""

    clean_project = str(project_id or "").strip()
    clean_order = str(order_id or "").strip()
    order = business.get_order(clean_order, project_id=clean_project)
    if order is None:
        raise PortfolioBundleError("order does not belong to this project")
    project_customer = str(business.project_customer(clean_project) or "").strip()
    order_customer = str(getattr(order, "customer_id", "") or "").strip()
    if not project_customer or project_customer != order_customer:
        raise PortfolioBundleError("order customer does not match the project customer scope")
    acceptance = business.acceptance(clean_order, project_id=clean_project)
    if acceptance is None or acceptance.status not in {"accepted", "accepted_with_exceptions"}:
        raise PortfolioBundleError("only a decided, accepted delivery can be exported")
    if not dict(acceptance.artifact_snapshot or {}):
        raise PortfolioBundleError("accepted delivery has no frozen artifact snapshot")
    if not business.verify_order_event_chain(clean_order):
        raise PortfolioBundleError("order event chain cannot be verified")

    snapshot = verify_delivery_snapshot(
        engine,
        project_id=clean_project,
        snapshot=dict(acceptance.artifact_snapshot),
    )
    source_files = sorted(
        list(snapshot.get("files", []) or []),
        key=lambda item: str(item.get("source_path", "")),
    )
    files: list[dict[str, Any]] = []
    for index, item in enumerate(source_files, start=1):
        files.append(
            {
                "member": _artifact_member(index, str(item.get("source_path", ""))),
                "size": int(item.get("size", 0) or 0),
                "sha256": str(item.get("sha256", "") or ""),
            }
        )
    events = business.events(clean_order)
    last_event = events[-1] if events else {}
    manifest: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "project_ref": _opaque_ref(clean_project),
        "order_ref": _opaque_ref(clean_order),
        "workflow_id": str(getattr(order, "workflow_id", "") or ""),
        "workflow_version": str(acceptance.workflow_version or ""),
        "acceptance": {
            "status": acceptance.status,
            "round_no": int(acceptance.round_no),
            "criteria_hash": str(acceptance.criteria_hash or ""),
            "evidence_hash": str(acceptance.evidence_hash or ""),
            "decided_at": str(acceptance.decided_at or ""),
            "exception_count": len(list(acceptance.exceptions or [])),
        },
        "event_chain": {
            "verified": True,
            "event_count": len(events),
            "head": str(dict(last_event.get("integrity", {}) or {}).get("event_hash", "") or ""),
        },
        "snapshot_manifest_sha256": str(snapshot.get("manifest_sha256", "") or ""),
        "files": files,
        "total_bytes": sum(int(item["size"]) for item in files),
    }
    manifest["bundle_digest"] = _digest_bytes(_canonical(manifest))

    root = workspace_root_for_project(engine, clean_project).resolve()
    export_root = root / "portfolio-exports" / _opaque_ref(clean_order)
    export_root.mkdir(parents=True, exist_ok=True)
    target = export_root / f"round-{int(acceptance.round_no)}-{manifest['bundle_digest'][:16]}.zip"
    with _target_export_lock(target):
        if target.is_file():
            existing = verify_portfolio_bundle(target)
            if existing != manifest:
                raise PortfolioBundleError("existing portfolio export does not match this delivery")
            return {
                "path": str(target),
                "sha256": _digest_file(target),
                "manifest": manifest,
                "reused": True,
            }

        # Two owner clicks (or a retried WebSocket request) must never share a
        # scratch file.  The per-target lock also prevents a Windows reader
        # from holding the final path while a second writer replaces it.
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with zipfile.ZipFile(temporary, "w") as archive:
                archive.writestr(_zip_info("manifest.json"), _canonical(manifest))
                for source, item in zip(source_files, files):
                    absolute = root / Path(str(source["archive_path"]))
                    with absolute.open("rb") as input_stream, archive.open(_zip_info(item["member"]), "w") as output_stream:
                        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            verify_portfolio_bundle(temporary)
            replace_with_retry(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return {
            "path": str(target),
            "sha256": _digest_file(target),
            "manifest": manifest,
            "reused": False,
        }
