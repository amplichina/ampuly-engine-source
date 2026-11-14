"""What a project remembers, for its owner to see and to delete (policy).

Memory is kept per project and, inside a project, per customer (``ampuly/layer5_memory/memory_partition.py``): a Markdown
file whose visible part is a list of entries, each opened by ``## [YYYY-MM-DD HH:MM]``. Previously, the console had
no way at all to look at it -- no action, no page -- so a stale note about one customer stayed a hint for every later
run, and the owner, whose ruling is that his material is kept until *he* deletes it, could not delete it.

This module reads those files and removes single entries, writing through the store's own save path -- the same one
that appends an entry -- and never another project's or another customer's file.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from ampuly.layer5_memory.markdown_memory import MarkdownMemoryStore

_ENTRY_HEAD = re.compile(r"^## \[(?P<at>[^\]\n]+)\][ \t]*$", re.MULTILINE)
# What memory_partition.customer_key produces: letters, digits, dot, underscore, hyphen. Anything else in a request is
# refused before it can name a path.
_CUSTOMER_KEY = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class MemoryAdminError(ValueError):
    """A request this module refuses; ``code`` is what the console reports."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _entry_id(at: str, text: str) -> str:
    return hashlib.sha256(f"{at}\n{text}".encode("utf-8")).hexdigest()[:16]


def split_entries(visible: str) -> tuple[str, list[dict[str, str]]]:
    """(the part before the first entry, the entries in order). Each entry: id, at, text."""
    heads = list(_ENTRY_HEAD.finditer(visible))
    if not heads:
        return visible.strip(), []
    preamble = visible[: heads[0].start()].strip()
    entries: list[dict[str, str]] = []
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(visible)
        text = visible[head.end():end].strip()
        at = head.group("at").strip()
        entries.append({"id": _entry_id(at, text), "at": at, "text": text})
    return preamble, entries


def join_entries(preamble: str, entries: list[dict[str, str]]) -> str:
    parts = [preamble.strip()] if preamble.strip() else []
    parts.extend(f"## [{entry['at']}]\n{entry['text']}".rstrip() for entry in entries)
    return "\n\n".join(parts)


def _project(project_id: str) -> str:
    project = str(project_id or "").strip()
    if not project or "/" in project or "\\" in project or project in {".", ".."}:
        raise MemoryAdminError("project_required", "a project id is required")
    return project


def _customer(customer: str) -> str:
    key = str(customer or "").strip()
    if key and (not _CUSTOMER_KEY.match(key) or not key.strip(".")):
        raise MemoryAdminError("customer_invalid", "not a customer key this product writes")
    return key


def _shown(entries: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [{**entry, "chars": len(entry["text"])} for entry in entries]


def memory_overview(ampuly_home: Path, project_id: str) -> dict[str, Any]:
    """Everything this project remembers: its own notes, then each customer's. Read only."""
    project = _project(project_id)
    store = MarkdownMemoryStore(Path(ampuly_home))
    _preamble, own = split_entries(store.load_visible_text(project))
    customers: list[dict[str, Any]] = []
    folder = store.customer_memory_dir(project)
    if folder.is_dir():
        for path in sorted(folder.glob("*.md")):
            if not _CUSTOMER_KEY.match(path.stem):
                continue
            _p, entries = split_entries(store.load_visible_text(project, path.stem))
            customers.append({"customer": path.stem, "entries": _shown(entries)})
    return {
        "project_id": project,
        "project": {"entries": _shown(own)},
        "customers": customers,
        "total": len(own) + sum(len(item["entries"]) for item in customers),
    }


def delete_memory_entry(ampuly_home: Path, project_id: str, *, customer: str = "", entry_id: str) -> dict[str, Any]:
    """Remove one entry from this project's own notes (``customer=""``) or from one customer's. Nothing else moves."""
    project = _project(project_id)
    key = _customer(customer)
    wanted = str(entry_id or "").strip()
    if not wanted:
        raise MemoryAdminError("entry_required", "which entry to delete was not said")
    store = MarkdownMemoryStore(Path(ampuly_home))
    path = store.memory_path(project, key)
    if not path.is_file():
        raise MemoryAdminError("entry_not_found", "there is no such memory")
    preamble, entries = split_entries(store.load_visible_text(project, key))
    kept = [entry for entry in entries if entry["id"] != wanted]
    if len(kept) == len(entries):
        raise MemoryAdminError("entry_not_found", "that entry is not there (it may have been changed or deleted already)")
    store.save_visible_text(join_entries(preamble, kept), project, key)
    return {"project_id": project, "customer": key, "deleted": wanted, "remaining": len(kept)}


# -- The console's card (4.7-A09): customers by name, and a copy in the recycle area before anything is deleted --

# Where a deleted entry's copy sits below one recycle batch. Restoring it from the recycle card puts the file at the
# same path under the project folder, where 资料 shows it: the words come back as a file, not into memory.
RECYCLED_FOLDER = "客户记忆"
NO_CUSTOMER_FOLDER = "不分客户"
_RECYCLE_ATTEMPTS = 8
_STAMP_UNSAFE = re.compile(r"[^0-9A-Za-z]+")


def name_customers(overview: dict[str, Any], roster: list[dict[str, str]]) -> dict[str, Any]:
    """The overview with a ``name`` on each customer: the roster's display name where the roster knows the customer,
    else the key the memory file is named by."""
    from ampuly.layer5_memory.memory_partition import customer_key

    names: dict[str, str] = {}
    for row in roster or []:
        customer_id = str((row or {}).get("customer_id", "") or "").strip()
        if customer_id:
            names.setdefault(customer_key(customer_id), str(row.get("display_name", "") or "").strip() or customer_id)
    customers = [{**item, "name": names.get(item["customer"], item["customer"])} for item in overview.get("customers", [])]
    return {**overview, "customers": customers}


def find_entry(ampuly_home: Path, project_id: str, *, customer: str = "", entry_id: str) -> dict[str, str]:
    """One entry as the overview showed it, or ``entry_not_found``. Read only."""
    project = _project(project_id)
    key = _customer(customer)
    wanted = str(entry_id or "").strip()
    if not wanted:
        raise MemoryAdminError("entry_required", "which entry to delete was not said")
    store = MarkdownMemoryStore(Path(ampuly_home))
    if not store.memory_path(project, key).is_file():
        raise MemoryAdminError("entry_not_found", "there is no such memory")
    _preamble, entries = split_entries(store.load_visible_text(project, key))
    for entry in entries:
        if entry["id"] == wanted:
            return entry
    raise MemoryAdminError("entry_not_found", "that entry is not there (it may have been changed or deleted already)")


def _linked(path: Path) -> bool:
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return True


def _recycled_words(project: str, key: str, entry: dict[str, str], deleted_at: datetime) -> str:
    return (
        "# 已删除的客户记忆\n\n"
        f"- 项目：{project}\n"
        f"- 客户：{key or '（不属于某个客户）'}\n"
        f"- 删除时间：{deleted_at:%Y-%m-%d %H:%M}\n\n"
        f"## [{entry['at']}]\n{entry['text']}\n"
    )


def recycle_entry(project_root: Path, project_id: str, *, customer: str = "", entry: dict[str, str]) -> dict[str, str]:
    """Write a copy of one entry into the project's recycle area, the way ``file_ops._recycle`` leaves a deleted file:
    ``<project folder>/.ampuly-recycle/<batch>/客户记忆/<customer>/<when>-<id>.md``. Never replaces anything.

    Returns ``id`` (what the recycle listing calls it) and ``restore_as`` (where restoring it puts the file, relative
    to the project folder).
    """
    from ampuly.layer4_tools.file_ops import RECYCLE_DIR_NAME

    project = _project(project_id)
    key = _customer(customer)
    root = Path(project_root)
    recycle = root / RECYCLE_DIR_NAME
    if _linked(root) or (root.exists() and not root.is_dir()):
        raise MemoryAdminError("recycle_unavailable", "the project folder is not a plain folder")
    if recycle.exists() and (_linked(recycle) or not recycle.is_dir()):
        raise MemoryAdminError("recycle_unavailable", "the recycle folder is not a plain folder")
    now = datetime.now()
    stamp = _STAMP_UNSAFE.sub("", str(entry.get("at", "")))[:12] or "entry"
    below = (RECYCLED_FOLDER, key or NO_CUSTOMER_FOLDER, f"{stamp}-{entry['id']}.md")
    try:
        recycle.mkdir(parents=True, exist_ok=True)
        marker = recycle / ".gitignore"
        if not marker.exists():
            # Self-ignoring, as file_ops keeps it: a model's git_commit must not put deleted words back in history.
            marker.write_text("*\n", encoding="utf-8")
        for _ in range(_RECYCLE_ATTEMPTS):
            batch = recycle / f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
            try:
                batch.mkdir()
            except FileExistsError:
                continue
            target = batch.joinpath(*below)
            target.parent.mkdir(parents=True)
            with open(target, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(_recycled_words(project, key, entry, now))
            return {"id": target.relative_to(root).as_posix(), "restore_as": "/".join(below)}
    except OSError as exc:
        raise MemoryAdminError("recycle_unavailable", "the copy could not be written to the recycle folder") from exc
    raise MemoryAdminError("recycle_unavailable", "no free name was found in the recycle folder")


def _take_back(project_root: Path, recycled_id: str) -> None:
    """Remove a copy this module has just written, and the folders it made for it, up to the recycle folder."""
    from ampuly.layer4_tools.file_ops import RECYCLE_DIR_NAME

    root = Path(project_root)
    path = root.joinpath(*recycled_id.split("/"))
    stop = root / RECYCLE_DIR_NAME
    try:
        path.unlink()
    except OSError:
        return
    folder = path.parent
    while folder != stop and stop in folder.parents:
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def delete_entry_to_recycle(
    ampuly_home: Path, project_root: Path, project_id: str, *, customer: str = "", entry_id: str,
) -> dict[str, Any]:
    """The console's delete: a copy of the entry goes to the recycle area first, then the entry leaves memory.

    A copy that cannot be written deletes nothing. A delete that is then refused (the entry changed in between) takes
    the copy back out, so the recycle area never holds words that were not deleted.
    """
    entry = find_entry(ampuly_home, project_id, customer=customer, entry_id=entry_id)
    recycled = recycle_entry(project_root, project_id, customer=customer, entry=entry)
    try:
        result = delete_memory_entry(ampuly_home, project_id, customer=customer, entry_id=entry_id)
    except Exception:
        _take_back(project_root, recycled["id"])
        raise
    return {**result, "recycled": recycled["id"], "restore_as": recycled["restore_as"]}
