"""What a restore would actually bring back, asked before anything moves.

Restoring is the operation people reach for when something has already gone
wrong, which is the worst moment to discover two things: that the restore will
be refused, and that it never contained what they were counting on.

The second is the important one here. A project backup holds the project's
own files, its workplace and its memory. It does **not** hold the business
record -- work orders, gates, budgets, deployments and their event chains live
in one store shared by every project, and no per-project archive can carry
them. Somebody restoring a deleted project to "get the orders back" is going
to get folders and no orders, and they should learn that here rather than
afterwards.

Nothing in this file writes, extracts or moves anything. It opens the archive
read-only, reads the manifest and the entry list, and answers.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

# What a project archive is allowed to contain, per the restore path. Listed
# here so the preflight refuses exactly what the restore would refuse, rather
# than approximating it.
# The same name the project service writes; kept here as a literal so the precheck imports nothing of the service.
BUSINESS_RECORDS_MEMBER = "business/records.json"
# 数据与文件 P3: the JSON members a v3 archive carries beside the three directory trees.
JSON_MEMBERS = (BUSINESS_RECORDS_MEMBER, "business/budgets.json", "business/history.json",
                "chat/messages.json", "config/approval_allowlist.json",
                # 这个项目的运行库：P4 起每个项目都用一致快照带走，不再跟着项目树按文件拷。
                "runtime/tasks.db")
MANIFEST_NAME = "manifest.json"

def not_included_from(manifest: dict) -> list[str] | None:
    """What THIS archive does not hold, read off its own coverage block; ``None`` for an archive without one.

    数据与文件 P3. The constant below is what a v2 archive gets -- and two of its lines had been false since
    orders and gate cards started travelling. An archive that states its own coverage is never out of date, so
    from v3 on the sentence comes from the archive rather than from a list somebody has to remember to edit.
    """
    coverage = manifest.get("coverage")
    if not isinstance(coverage, dict) or not coverage:
        return None
    from ampuly.core.data_map import BY_KEY

    missing: list[str] = []
    for key, state in coverage.items():
        if not isinstance(state, dict) or str(state.get("state", "")) != "excluded":
            continue
        entry = BY_KEY.get(key)
        label = entry.label_zh if entry is not None else key
        reason = str(state.get("reason", "") or "")
        missing.append(f"{label}（{reason}）" if reason else label)
    # Carried but not put back by a restore: worth saying, because 「在备份里」 and 「还原后就回来了」 are not
    # the same promise, and only one of them is true for these.
    #
    # 这里不再自己列是哪几样。原来写死的那三样里，「聊天」在 2026-09-24 已经变成会还原的了，而这行字还在
    # 告诉人它不会——名单写在两个地方，改了一边忘了另一边，就会出这种假话。现在归档自己说：它带着哪些、
    # 哪些会放回去、不放的理由是什么，这段只负责把它读出来。
    for key, state in coverage.items():
        if not isinstance(state, dict) or state.get("restored") is not False or state.get("state") != "included":
            continue
        entry = BY_KEY.get(key)
        label = str(state.get("label_zh") or (entry.label_zh if entry is not None else key))
        note = str(state.get("restore_note", "") or "")
        missing.append(f"{label}（在这份备份里，但还原不会自动放回去；{note}）" if note
                       else f"{label}（在这份备份里，但还原不会自动放回去）")
    return missing


# Business truth that lives outside any single project's archive. Only a v2 archive uses this; from v3 on the
# archive states its own coverage (:func:`not_included_from`).
NOT_INCLUDED = (
    "work orders and their event chain",
    "human gates and acceptance decisions",
    "run budgets and their reservations",
    "solution packages and the deployment manifest",
    "credential references",
    "accounts and sessions",
    # Deleting a project removes its console conversations from the console's own store; no archive holds them.
    "the console conversation history of this project",
)


def _stamp(value: str) -> str:
    """Turn the archive's compact timestamp into something a person reads."""
    raw = str(value or "").strip()
    for pattern in ("%Y%m%dT%H%M%S%f", "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(raw, pattern).isoformat(timespec="seconds")
        except ValueError:
            continue
    return raw


def inspect_backup(
    archive_path: str | Path,
    *,
    ampuly_home: Path,
    project_id: str = "",
    project_exists: Any = None,
) -> dict[str, Any]:
    """Say what this archive is, whether it would restore, and what it lacks.

    ``refusals`` lists every reason the restore would be turned away, not the
    first one: fixing one problem only to meet the next is how a bad hour gets
    longer. An empty list is not a promise the restore will succeed -- disks
    fill, files move -- so the field is called ``would_be_refused`` rather than
    anything that sounds like a guarantee.
    """
    source = Path(archive_path).resolve()
    backup_root = (Path(ampuly_home) / "backups" / "projects").resolve()
    report: dict[str, Any] = {
        "archive": str(source),
        "archive_name": source.name,
        "project_id": "",
        "taken_at": "",
        "entries": 0,
        "bytes": 0,
        "has_workplace": False,
        "has_memory": False,
        # True only when the archive carries per-file digests and they were checked. Archives older than that
        # format restore without any content check, which is not something to put behind one click.
        "content_verified": False,
        "refusals": [],
        # One entry per refusal, in the same order: a stable code and the values the sentence names, so the page
        # can say it in the reader's language. The English sentence stays for logs and for codes a page predates.
        "refusal_details": [],
        "not_included": list(NOT_INCLUDED),   # 有 coverage 的备份在读到 manifest 后会覆盖它
    }
    refusals: list[str] = report["refusals"]

    def refuse(code: str, sentence: str, **values: str) -> None:
        refusals.append(sentence)
        report["refusal_details"].append({"code": code, **{key: str(value) for key, value in values.items()}})

    try:
        source.relative_to(backup_root)
    except ValueError:
        refuse("outside_backup_folder", "this file is not inside .ampuly/backups/projects, and a restore only reads from there")
    if not source.is_file():
        refuse("no_such_file", "there is no such file")
        report["would_be_refused"] = True
        return report
    report["bytes"] = source.stat().st_size

    try:
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            report["entries"] = len(names)
            if MANIFEST_NAME not in names:
                refuse("no_manifest", "the archive has no manifest, so nothing says which project it is")
                report["would_be_refused"] = True
                return report
            try:
                manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                refuse("manifest_unreadable", "the manifest is unreadable")
                report["would_be_refused"] = True
                return report
            schema_version = int(manifest.get("schema_version", 0) or 0)
            report["content_verified"] = schema_version >= 2
            if schema_version >= 2:
                body = {
                    key: value for key, value in manifest.items()
                    if key != "content_digest"
                }
                digest = hashlib.sha256(
                    json.dumps(
                        body,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if digest != str(manifest.get("content_digest", "")):
                    refuse("manifest_digest_mismatch", "the manifest digest does not match its content")
                declared = list(manifest.get("files", []) or [])
                declared_names = [str(item.get("path", "")) for item in declared]
                if names != [MANIFEST_NAME, *declared_names]:
                    refuse("member_list_mismatch", "the archive member list does not match its manifest")
                else:
                    for item in declared:
                        member = str(item.get("path", ""))
                        info = archive.getinfo(member)
                        if info.file_size != int(item.get("size", -1)):
                            refuse("size_wrong", f"the archived size is wrong: {member}", member=member)
                            break
                        member_digest = hashlib.sha256()
                        with archive.open(info) as stream:
                            while chunk := stream.read(1024 * 1024):
                                member_digest.update(chunk)
                        if member_digest.hexdigest() != str(item.get("sha256", "")):
                            refuse("content_changed", f"the archived content was changed: {member}", member=member)
                            break
            manifest_id = str(manifest.get("project_id", "") or "").strip()
            report["project_id"] = manifest_id
            report["taken_at"] = _stamp(str(manifest.get("created_at", "") or ""))
            # 数据与文件 P3: an archive that states its own coverage says what it does not hold; the
            # constant is only for the older ones, and two of its lines had been false for a while.
            stated = not_included_from(manifest)
            if stated is not None:
                report["not_included"] = stated
                report["coverage"] = manifest.get("coverage")
            wanted = str(project_id or manifest_id).strip()
            if not wanted or wanted == "default":
                refuse("project_name_not_restorable", "the project name in this archive cannot be restored")
            elif manifest_id and manifest_id != wanted:
                refuse("holds_another_project", f"this archive holds {manifest_id!r}, not {wanted!r}", holds=manifest_id, wanted=wanted)
            prefixes = (f"project/{wanted}/", f"workplace/{wanted}/", f"memory/{wanted}.md",
                        f"memory/{wanted}/customers/")
            for name in names:
                normalized = str(name).replace("\\", "/")
                if normalized.startswith("/") or ".." in Path(normalized).parts:
                    refuse("path_climbs_out", "the archive contains a path that climbs out of it")
                    break
                # One more member belongs: the project's business records (orders, their chains, gate cards), which
                # live in the shared business store rather than under the project's folders.
                if name != MANIFEST_NAME and normalized not in JSON_MEMBERS and not any(normalized.startswith(p) for p in prefixes):
                    refuse("path_does_not_belong", f"the archive contains a path that does not belong to it: {normalized}", path=normalized)
                    break
                if (archive.getinfo(name).external_attr >> 16) & 0o170000 == 0o120000:
                    refuse("symbolic_link", "the archive contains a symbolic link")
                    break
            report["has_workplace"] = any(str(n).replace("\\", "/").startswith(f"workplace/{wanted}/") for n in names)
            report["has_memory"] = f"memory/{wanted}.md" in [str(n).replace("\\", "/") for n in names]
    except (OSError, zipfile.BadZipFile) as exc:
        refuse("archive_unreadable", f"the archive could not be read: {exc}", detail=str(exc))
        report["would_be_refused"] = True
        return report

    if callable(project_exists) and report["project_id"]:
        try:
            occupied = project_exists(str(project_id or report["project_id"]))
            if occupied:
                # The restore refuses if any of the three places it writes to holds something; a project folder
                # that is gone does not mean its workplace or memory file went with it.
                where = f" ({', '.join(str(item) for item in occupied)})" if isinstance(occupied, (list, tuple)) else ""
                refuse(
                    "project_exists",
                    f"a project by this name already exists{where}; a restore never writes over one, "
                    "so this would be refused until the existing project is dealt with",
                    where=", ".join(str(item) for item in occupied) if isinstance(occupied, (list, tuple)) else "",
                )
        except Exception:
            refuse("existence_unknown", "whether a project by this name already exists could not be established")

    report["would_be_refused"] = bool(refusals)
    return report
