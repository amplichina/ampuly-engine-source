"""Immutable, bounded delivery-round snapshots inside a project workplace.

The live workplace remains the operational filesystem truth.  Before a
contractual acceptance round is decided (or reopened), this module copies only
the files that were named as acceptance evidence into a round-specific archive.
The signed business event stores the returned manifest reference and digest;
this directory is the immutable body that reference describes.

A file is found by the console preview's path rule -- inside the workplace, no
link, no hidden or runtime-internal name, no second name -- but it is never read
through the preview (C5-2, 2026-09-26): the preview stops a picture at 8 MB, and
that limit made a whole acceptance decision fail on one large image.  A round is
bounded by its own limits instead (``MAX_FILES`` files, ``MAX_FILE_BYTES`` each,
``MAX_TOTAL_BYTES`` in all).  What cannot be frozen, bound or verified raises a
``SnapshotError`` naming why (``reason``) and which file (``path``), so the
acceptance round (``acceptance_round``) can say it in the owner's words.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ampuly.plugins.office_ui.artifacts_service import (
    _hidden,
    artifact_path_parts,
    multiply_linked,
    redirected,
    workspace_root_for_project,
)
from ampuly.core.config import replace_with_retry

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ROUND_NAME = re.compile(r"^round-[1-9][0-9]{0,8}$")
_ARCHIVE_ROOT = "delivery-history"
_EXPORT_ROOT = "delivery-exports"
MAX_FILES = 100
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024

# Why a round cannot be frozen, a file bound or a snapshot verified (``SnapshotError.reason``).
TOO_MANY = "too_many"                # more files than one round holds
FILE_TOO_LARGE = "file_too_large"    # one file over MAX_FILE_BYTES
ROUND_TOO_LARGE = "round_too_large"  # the files together over MAX_TOTAL_BYTES
CHANGED = "changed"                  # the file is no longer what its evidence was bound to
CHANGING = "changing"                # the file changed while it was being read
MISSING = "missing"                  # no such file
UNREADABLE = "unreadable"            # a link, a hidden or runtime-internal name, a second name, outside the workplace
CONFLICT = "conflict"                # the round already holds other files
MISMATCH = "mismatch"                # a frozen copy no longer matches its manifest
INVALID = "invalid"                  # a request or a reference that names nothing
STUCK = "stuck"                      # a round nothing refers to could not be removed

# The same file compared the way the machine compares names.
_fold = os.path.normcase


class SnapshotError(ValueError):
    """A round that cannot be frozen, a file that cannot be bound, a snapshot that does not verify.

    The message stays technical (and matchable); ``reason`` and ``path`` (as the workplace spells it) are for the
    owner's words. ``size``/``limit`` carry the numbers of a limit that was hit.
    """

    def __init__(self, message: str, *, reason: str, path: str = "", size: int = 0, limit: int = 0) -> None:
        super().__init__(message)
        self.reason = reason
        self.path = path
        self.size = size
        self.limit = limit


@dataclass(frozen=True)
class _Located:
    path: Path       # the file on disk
    relative: str    # as the workplace spells it (POSIX)
    size: int


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_plain_directory(path: Path, boundary: Path) -> None:
    current = boundary
    for part in path.relative_to(boundary).parts:
        current = current / part
        if current.exists() and (
            current.is_symlink() or getattr(current, "is_junction", lambda: False)()
        ):
            raise SnapshotError("artifact history contains a redirected directory", reason=UNREADABLE)


def _locate(root: Path, relative_path: str) -> _Located:
    """One workplace file by the preview's path rule (``artifacts_service.preview_file_under``), without reading it."""
    candidate = str(relative_path or "").replace("\\", "/")
    try:
        parts = artifact_path_parts(candidate)
    except ValueError as exc:
        raise SnapshotError(f"path outside the project workspace or noncanonical: {candidate}",
                            reason=UNREADABLE, path=candidate) from exc
    if redirected(root):
        raise SnapshotError("artifact path contains a redirected component", reason=UNREADABLE, path=candidate)
    target = root
    for part in parts:
        target = target / part
        if redirected(target):
            raise SnapshotError("artifact path contains a redirected component", reason=UNREADABLE, path=candidate)
    target = target.resolve()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise SnapshotError("path outside the workspace", reason=UNREADABLE, path=candidate) from exc
    if _hidden(relative):
        raise SnapshotError("path is runtime-internal", reason=UNREADABLE, path=candidate)
    if not target.is_file():
        raise SnapshotError(f"artifact not found: {candidate}", reason=MISSING, path=candidate)
    if multiply_linked(target):
        raise SnapshotError("multiply-linked file", reason=UNREADABLE, path=candidate)
    return _Located(target, relative.as_posix(), int(target.stat().st_size))


def bind_artifact_evidence(engine: Any, project_id: str, relative_path: str) -> dict[str, Any]:
    """Bind one workplace file to acceptance evidence by content.

    ``artifacts_service.artifact_evidence`` without the preview, so a picture over the preview's 8 MB is bound like any
    other file; a file that could never be frozen (over ``MAX_FILE_BYTES``) is refused here rather than at the decision.
    """
    root = workspace_root_for_project(engine, project_id).resolve()
    found = _locate(root, relative_path)
    if found.size > MAX_FILE_BYTES:
        raise SnapshotError(f"artifact exceeds per-file snapshot limit: {found.relative}", reason=FILE_TOO_LARGE,
                            path=found.relative, size=found.size, limit=MAX_FILE_BYTES)
    before = found.path.stat()
    digest = _digest_file(found.path)
    after = found.path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise SnapshotError("artifact changed while acceptance evidence was recorded", reason=CHANGING,
                            path=found.relative)
    return {
        "artifact_id": f"sha256:{digest}",
        "artifact_hash": digest,
        "artifact_path": found.relative,
        "artifact_size": int(after.st_size),
    }


def _reference(root: Path, manifest_path: Path, raw: bytes, manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "manifest_path": manifest_path.relative_to(root).as_posix(),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "file_count": len(list(manifest.get("files", []) or [])),
        "total_bytes": int(manifest.get("total_bytes", 0) or 0),
    }


def _existing_round(
    engine: Any, project_id: str, root: Path, target: Path, paths: list[str], expected: dict[str, str]
) -> dict[str, Any] | None:
    """The reference of the round already at ``target`` when it is exactly this one: the same files, every frozen copy
    verified, each as its evidence was bound. None for anything else."""
    manifest_path = target / "manifest.json"
    try:
        if redirected(target) or not target.is_dir() or not manifest_path.is_file():
            return None
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
        if not isinstance(manifest, dict) or list(manifest.get("source_paths", []) or []) != paths:
            return None
        reference = _reference(root, manifest_path, raw, manifest)
        verified = verify_delivery_snapshot(engine, project_id=project_id, snapshot=reference)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    digests = {_fold(str(item["source_path"])): str(item["sha256"]) for item in verified["files"]}
    if any(digests.get(key) != digest for key, digest in expected.items()):
        return None
    return reference


def _remove_round(target: Path) -> None:
    """Remove a round nothing refers to. Never through a link: the caller has proved the folder plain."""
    try:
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    except OSError as exc:
        raise SnapshotError("an unreferenced artifact history round could not be removed", reason=STUCK) from exc


def _remove_leftovers(target: Path) -> None:
    """The half-written copies an interrupted attempt at this round left beside it (hidden, never referenced)."""
    parent = target.parent
    if not parent.is_dir():
        return
    for leftover in parent.glob(f".{target.name}-*.tmp"):
        if leftover.is_dir() and not redirected(leftover):
            shutil.rmtree(leftover, ignore_errors=True)


def freeze_delivery_round(
    engine: Any,
    *,
    project_id: str,
    order_id: str,
    round_no: int,
    artifact_paths: list[str],
    expected_hashes: dict[str, str] | None = None,
    replace_unreferenced: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Freeze one round's evidence files; return ``(reference, created)``.

    A round already on disk that is exactly this one (the same files, each frozen copy verified and as its evidence
    was bound) is returned as it is, ``created`` False. Any other round already there is refused -- unless
    ``replace_unreferenced``: the caller knows no recorded decision refers to this round (an attempt froze it and then
    failed), so it is removed and frozen again, together with any copy an interrupted attempt left half-written.
    """
    clean_order = str(order_id or "").strip()
    if not _SAFE_ID.fullmatch(clean_order):
        raise SnapshotError("invalid order id for artifact history", reason=INVALID)
    clean_round = int(round_no)
    if clean_round < 1:
        raise SnapshotError("artifact history round must be positive", reason=INVALID)
    paths = list(dict.fromkeys(str(item or "").strip().replace("\\", "/") for item in artifact_paths if str(item or "").strip()))
    if not paths:
        raise SnapshotError("acceptance rework requires path-backed artifact evidence", reason=INVALID)
    if len(paths) > MAX_FILES:
        raise SnapshotError("too many artifacts in one acceptance round", reason=TOO_MANY,
                            size=len(paths), limit=MAX_FILES)
    for path in paths:
        if path == _ARCHIVE_ROOT or path.startswith(f"{_ARCHIVE_ROOT}/"):
            raise SnapshotError("an artifact history snapshot cannot snapshot itself", reason=UNREADABLE, path=path)
    expected = {
        _fold(str(path or "").strip().replace("\\", "/")): str(digest or "").strip().lower()
        for path, digest in dict(expected_hashes or {}).items()
        if str(path or "").strip() and str(digest or "").strip()
    }

    root = workspace_root_for_project(engine, project_id).resolve()
    history_root = root / _ARCHIVE_ROOT
    target = history_root / clean_order / f"round-{clean_round}"
    _ensure_plain_directory(target, root)
    if target.exists():
        existing = _existing_round(engine, project_id, root, target, paths, expected)
        if existing is not None:
            return existing, False
        if not replace_unreferenced:
            raise SnapshotError("artifact history round already exists with different files", reason=CONFLICT)
        _remove_round(target)
    if replace_unreferenced:
        _remove_leftovers(target)

    sources: list[_Located] = []
    total = 0
    for relative in paths:
        found = _locate(root, relative)
        if found.size > MAX_FILE_BYTES:
            raise SnapshotError(f"artifact exceeds per-file snapshot limit: {relative}", reason=FILE_TOO_LARGE,
                                path=found.relative, size=found.size, limit=MAX_FILE_BYTES)
        total += found.size
        if total > MAX_TOTAL_BYTES:
            raise SnapshotError("acceptance round exceeds snapshot byte limit", reason=ROUND_TOO_LARGE,
                                size=total, limit=MAX_TOTAL_BYTES)
        sources.append(found)

    target.parent.mkdir(parents=True, exist_ok=True)
    # The staging copy sits beside the round and holds the same deep relative paths, so its own name is the only
    # length it adds: 12 hex digits, not 32 -- on Windows a 32-digit name pushed paths that fit as round-N past
    # MAX_PATH ("文件名或扩展名太长"), and the customer's 通过 was refused for a path the round itself could hold.
    temporary = target.parent / f".{target.name}-{uuid.uuid4().hex[:12]}.tmp"
    temporary.mkdir()
    try:
        files: list[dict[str, Any]] = []
        total = 0
        for found in sources:
            relative = Path(found.relative)
            destination = temporary / "files" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(found.path, destination)
            content_hash = _digest_file(destination)
            expected_hash = expected.get(_fold(relative.as_posix()), "")
            if expected_hash and content_hash != expected_hash:
                raise SnapshotError(f"artifact changed since acceptance evidence: {relative.as_posix()}",
                                    reason=CHANGED, path=relative.as_posix())
            size = int(destination.stat().st_size)
            total += size
            files.append({
                "source_path": relative.as_posix(),
                "archive_path": (Path("files") / relative).as_posix(),
                "size": size,
                "sha256": content_hash,
            })
        manifest = {
            "schema_version": 1,
            "order_id": clean_order,
            "round_no": clean_round,
            "source_paths": paths,
            "files": files,
            "total_bytes": total,
        }
        raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        (temporary / "manifest.json").write_bytes(raw)
        replace_with_retry(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return _reference(root, target / "manifest.json", raw, manifest), True


def snapshot_delivery_round(
    engine: Any,
    *,
    project_id: str,
    order_id: str,
    round_no: int,
    artifact_paths: list[str],
    expected_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Copy evidence files into one immutable round and return its manifest.

    Existing round directories are never overwritten.  An exact retry (the same
    files, verified, each as its evidence was bound) reads the already-written
    manifest, while a conflicting retry fails closed.
    """
    reference, _created = freeze_delivery_round(
        engine,
        project_id=project_id,
        order_id=order_id,
        round_no=round_no,
        artifact_paths=artifact_paths,
        expected_hashes=expected_hashes,
    )
    return reference


def discard_delivery_round(engine: Any, *, project_id: str, snapshot: dict[str, Any]) -> bool:
    """Remove a round an attempt froze and then could not record -- only while it is still exactly that round.

    Returns whether it was removed. A reference that names no round, or a round whose manifest is no longer the one
    referenced (another attempt replaced it), is left alone.
    """
    reference = dict(snapshot or {})
    manifest_path = str(reference.get("manifest_path", "") or "").strip().replace("\\", "/")
    expected = str(reference.get("manifest_sha256", "") or "").strip().lower()
    parts = PurePosixPath(manifest_path).parts
    if (
        not expected
        or len(parts) != 4
        or parts[0] != _ARCHIVE_ROOT
        or not _SAFE_ID.fullmatch(parts[1])
        or not _ROUND_NAME.fullmatch(parts[2])
        or parts[3] != "manifest.json"
    ):
        return False
    root = workspace_root_for_project(engine, project_id).resolve()
    target = root / parts[0] / parts[1] / parts[2]
    try:
        _ensure_plain_directory(target, root)
        raw = (target / "manifest.json").read_bytes()
    except (OSError, ValueError):
        return False
    if hashlib.sha256(raw).hexdigest() != expected:
        return False
    _remove_round(target)
    return True


def verify_delivery_snapshot(
    engine: Any,
    *,
    project_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Return preview paths only after verifying the manifest and every file."""
    reference = dict(snapshot or {})
    manifest_path = str(reference.get("manifest_path", "") or "").strip().replace("\\", "/")
    expected_manifest = str(reference.get("manifest_sha256", "") or "").strip().lower()
    if not manifest_path.startswith(f"{_ARCHIVE_ROOT}/") or not expected_manifest:
        raise SnapshotError("artifact snapshot reference is incomplete", reason=INVALID)
    root = workspace_root_for_project(engine, project_id).resolve()
    raw = _locate(root, manifest_path).path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_manifest:
        raise SnapshotError("artifact snapshot manifest digest mismatch", reason=MISMATCH)
    manifest = json.loads(raw.decode("utf-8"))
    if not isinstance(manifest, dict) or int(manifest.get("schema_version", 0) or 0) != 1:
        raise SnapshotError("unsupported artifact snapshot manifest", reason=MISMATCH)
    manifest_dir = Path(manifest_path).parent
    verified_files: list[dict[str, Any]] = []
    total = 0
    for item in list(manifest.get("files", []) or []):
        if not isinstance(item, dict):
            raise SnapshotError("artifact snapshot file record is invalid", reason=MISMATCH)
        archive_relative = str(item.get("archive_path", "") or "").strip().replace("\\", "/")
        source_relative = str(item.get("source_path", "") or "").strip().replace("\\", "/")
        expected_hash = str(item.get("sha256", "") or "").strip().lower()
        if not archive_relative.startswith("files/") or not source_relative or not expected_hash:
            raise SnapshotError("artifact snapshot file record is incomplete", reason=MISMATCH)
        project_relative = (manifest_dir / Path(archive_relative)).as_posix()
        archived = _locate(root, project_relative)
        actual_hash = _digest_file(archived.path)
        if actual_hash != expected_hash:
            raise SnapshotError(f"artifact snapshot file digest mismatch: {source_relative}", reason=MISMATCH,
                                path=source_relative)
        size = archived.size
        if size != int(item.get("size", -1)):
            raise SnapshotError(f"artifact snapshot file size mismatch: {source_relative}", reason=MISMATCH,
                                path=source_relative)
        total += size
        verified_files.append({
            "source_path": source_relative,
            "archive_path": project_relative,
            "size": size,
            "sha256": actual_hash,
        })
    if total != int(manifest.get("total_bytes", -1)):
        raise SnapshotError("artifact snapshot total byte count mismatch", reason=MISMATCH)
    return {
        **reference,
        "verified": True,
        "order_id": str(manifest.get("order_id", "") or ""),
        "round_no": int(manifest.get("round_no", 0) or 0),
        "files": verified_files,
    }


def compare_delivery_snapshots(
    engine: Any,
    *,
    project_id: str,
    earlier: dict[str, Any],
    later: dict[str, Any],
) -> dict[str, Any]:
    """Compare two verified rounds by path, size and content digest.

    This is deliberately a byte-level inventory comparison.  It does not
    claim visual, semantic or quality equivalence.
    """

    before = verify_delivery_snapshot(
        engine, project_id=project_id, snapshot=earlier
    )
    after = verify_delivery_snapshot(
        engine, project_id=project_id, snapshot=later
    )
    before_order = str(before.get("order_id", "") or "").strip()
    after_order = str(after.get("order_id", "") or "").strip()
    if not before_order or before_order != after_order:
        raise ValueError("artifact snapshot comparison requires the same order")
    before_round = int(before.get("round_no", 0) or 0)
    after_round = int(after.get("round_no", 0) or 0)
    if before_round >= after_round:
        raise ValueError("artifact snapshot comparison requires increasing rounds")

    before_files = {
        str(item["source_path"]): item for item in list(before["files"])
    }
    after_files = {
        str(item["source_path"]): item for item in list(after["files"])
    }
    before_paths = set(before_files)
    after_paths = set(after_files)
    common = sorted(before_paths & after_paths)
    changed: list[dict[str, Any]] = []
    unchanged: list[str] = []
    for path in common:
        left = before_files[path]
        right = after_files[path]
        if left["sha256"] == right["sha256"] and left["size"] == right["size"]:
            unchanged.append(path)
            continue
        changed.append(
            {
                "source_path": path,
                "before_sha256": left["sha256"],
                "after_sha256": right["sha256"],
                "before_size": left["size"],
                "after_size": right["size"],
            }
        )
    return {
        "comparison": "byte_inventory",
        "order_id": before_order,
        "earlier_round": before_round,
        "later_round": after_round,
        "added": sorted(after_paths - before_paths),
        "removed": sorted(before_paths - after_paths),
        "changed": changed,
        "unchanged": unchanged,
    }


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _digest_stream(stream: Any) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _export_member(source_path: str) -> str:
    clean = str(source_path or "").strip().replace("\\", "/")
    relative = PurePosixPath(clean)
    if (
        not clean
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("artifact snapshot export member is unsafe")
    return f"files/{relative.as_posix()}"


def _verify_delivery_export(
    path: Path,
    *,
    manifest_digest: str,
    files: list[dict[str, Any]],
) -> None:
    expected_names = ["manifest.json"] + [
        _export_member(str(item["source_path"]))
        for item in sorted(files, key=lambda row: str(row["source_path"]))
    ]
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.namelist() != expected_names:
                raise ValueError("artifact snapshot export members mismatch")
            with archive.open("manifest.json") as stream:
                if _digest_stream(stream) != manifest_digest:
                    raise ValueError("artifact snapshot export manifest mismatch")
            for item, member in zip(
                sorted(files, key=lambda row: str(row["source_path"])),
                expected_names[1:],
            ):
                info = archive.getinfo(member)
                if info.file_size != int(item["size"]):
                    raise ValueError(
                        f"artifact snapshot export size mismatch: {item['source_path']}"
                    )
                with archive.open(info) as stream:
                    if _digest_stream(stream) != str(item["sha256"]):
                        raise ValueError(
                            f"artifact snapshot export digest mismatch: {item['source_path']}"
                        )
    except zipfile.BadZipFile as exc:
        raise ValueError("artifact snapshot export is not a valid ZIP") from exc


def export_delivery_snapshot(
    engine: Any,
    *,
    project_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Write one verified round as a deterministic, scope-bound ZIP."""

    verified = verify_delivery_snapshot(
        engine, project_id=project_id, snapshot=snapshot
    )
    order_id = str(verified.get("order_id", "") or "").strip()
    round_no = int(verified.get("round_no", 0) or 0)
    if not _SAFE_ID.fullmatch(order_id) or round_no < 1:
        raise ValueError("artifact snapshot export identity is invalid")
    manifest_digest = str(
        verified.get("manifest_sha256", "") or ""
    ).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_digest):
        raise ValueError("artifact snapshot export manifest digest is invalid")

    root = workspace_root_for_project(engine, project_id).resolve()
    export_dir = root / _EXPORT_ROOT / order_id
    _ensure_plain_directory(export_dir, root)
    export_dir.mkdir(parents=True, exist_ok=True)
    destination = export_dir / f"round-{round_no}-{manifest_digest[:12]}.zip"
    if destination.exists():
        if destination.is_symlink() or getattr(
            destination, "is_junction", lambda: False
        )():
            raise ValueError("artifact snapshot export is redirected")
        _verify_delivery_export(
            destination,
            manifest_digest=manifest_digest,
            files=list(verified["files"]),
        )
        return {
            "path": destination.relative_to(root).as_posix(),
            "sha256": _digest_file(destination),
            "file_count": len(list(verified["files"])),
            "total_bytes": sum(
                int(item["size"]) for item in list(verified["files"])
            ),
            "round_no": round_no,
            "order_id": order_id,
        }

    manifest_source = _locate(root, str(verified["manifest_path"])).path
    # Same as the round's staging copy: the temporary file's own name is the only length it adds beside the
    # export, so 12 hex digits, not 32 -- on Windows a 32-digit name could push a path that fits as the export
    # past MAX_PATH ("文件名或扩展名太长"). It is removed by name on failure, so no pattern depends on its length.
    temporary = export_dir / f".{destination.name}-{uuid.uuid4().hex[:12]}.tmp"
    try:
        with zipfile.ZipFile(temporary, mode="x", allowZip64=True) as archive:
            with archive.open(_zip_info("manifest.json"), mode="w") as output:
                with manifest_source.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            for item in sorted(
                list(verified["files"]), key=lambda row: str(row["source_path"])
            ):
                archived = _locate(root, str(item["archive_path"]))
                member = _export_member(str(item["source_path"]))
                with archive.open(_zip_info(member), mode="w") as output:
                    with archived.path.open("rb") as source:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
        replace_with_retry(temporary, destination)
        _verify_delivery_export(
            destination,
            manifest_digest=manifest_digest,
            files=list(verified["files"]),
        )
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "path": destination.relative_to(root).as_posix(),
        "sha256": _digest_file(destination),
        "file_count": len(list(verified["files"])),
        "total_bytes": sum(int(item["size"]) for item in list(verified["files"])),
        "round_no": round_no,
        "order_id": order_id,
    }
