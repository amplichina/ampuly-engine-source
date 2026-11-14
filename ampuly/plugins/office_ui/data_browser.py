"""Bounded, link-free readers for the 「资料」 entry -- data phase 2, D5.

Everything the entry shows comes from here: a folder's page, a file's preview, an order's view, the attachment list
and a project's space usage. The rules the whole entry rests on are kept in one place:

* **A folder is opened only through** ``confined_dir`` (D1): no component is a link, and the real path below the real
  base is spelled the way it was asked for. The real path is checked again after a scan; a mismatch throws the page
  away rather than showing names from a folder that changed under it.
* **File content is read only through** ``SecureWorkspace.open_read`` (D2): the name opened is the name asked for.
* **Nothing here writes.** No directory is created, nothing is renamed, replaced or removed. A missing folder answers
  "it is not there" -- it is never made.
* **Every read is bounded**, in entries and in seconds, and says so when it stopped early. A refusal is a code
  (``DataRefused``), never an OS message and never a path: the console shows codes, and a path would leak where the
  data area is.

Synchronous on purpose: the handlers call it through ``asyncio.to_thread`` so a large folder cannot hold the socket.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from ampuly.core.order_layout import (
    DELIVERY_EXPORTS,
    DELIVERY_HISTORY,
    DOWNLOADS,
    LEGACY_DOWNLOADS,
    OUTPUT,
    PORTFOLIO_EXPORTS,
    STEPS,
    WORK,
    step_segment,
)
from ampuly.layer4_tools.workspace_fs import SecureWorkspace, WorkspaceBoundaryError
from ampuly.plugins.office_ui import order_folder
from ampuly.plugins.office_ui.artifacts_service import (
    _hidden,
    _kind_for,
    artifact_path_parts,
    confined_dir,
    redirected,
    redirected_entry,
)

# What one call may look at before it answers "there was more".
PAGE = 200
LIST_SCAN_CAP = 20_000
COUNT_CAP = 1_000
ORDER_ROWS = 500
ORDER_FOLDERS = 2_000
ORDER_VIEW_ENTRIES = 20_000
ORDER_VIEW_SECONDS = 1.0
GLOB_SCAN_CAP = 1_000
ATTACHMENT_FOLDERS = 5_000
STEP_TABLE_BYTES = order_folder.STEP_TABLE_BYTES
STEP_TABLE_STEPS = 200
STEP_TABLE_WRITES = 50
STEP_TABLE_DELIVERABLES = 50
DELIVERABLE_CHARS = 300
USAGE_ENTRIES = 200_000
USAGE_SECONDS = 2.0
DOWNLOAD_BYTES = 512 * 1024 * 1024
PREVIEW_TEXT_BYTES = 60_000
PREVIEW_IMAGE_BYTES = 8 * 1024 * 1024

_ATTACHMENT_FOLDER = re.compile(r"^[0-9a-f]{16}$")


class DataRefused(Exception):
    """A read the 资料 entry will not do. ``code`` is what the console shows; no path, no OS text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = str(code or "unreadable")


@dataclass
class Budget:
    """What one answer may spend: entries looked at, and seconds. ``stop`` ends it from outside."""

    entries: int = ORDER_VIEW_ENTRIES
    seconds: float = ORDER_VIEW_SECONDS
    stop: Callable[[], bool] | None = None
    _started: float = field(default_factory=time.monotonic)
    spent: int = 0

    def take(self, entries: int = 1) -> bool:
        """Spend ``entries``; False once this answer must stop."""
        self.spent += int(entries)
        return self.left

    @property
    def left(self) -> bool:
        if self.spent >= self.entries or (time.monotonic() - self._started) >= self.seconds:
            return False
        return not (self.stop is not None and self.stop())


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds).isoformat(timespec="seconds")


def _entry_facts(entry: os.DirEntry) -> tuple[bool, int, float]:
    """(is a directory, size, modified) from the scan's own stat -- no extra system call on Windows."""
    try:
        info = entry.stat(follow_symlinks=False)
        return entry.is_dir(follow_symlinks=False), int(info.st_size), float(info.st_mtime)
    except OSError:
        return False, 0, 0.0


def _real(path: Path) -> str:
    try:
        return os.path.realpath(path)
    except OSError as exc:  # noqa: BLE001 - unreadable is not readable
        raise DataRefused("unreadable") from exc


def _open_dir(base: Path, parts: tuple[str, ...]) -> Path:
    try:
        return confined_dir(base, parts)
    except ValueError as exc:
        raise DataRefused("refused_link" if "link" in str(exc) else "invalid_path") from exc


def list_dir(base: Path, parts: tuple[str, ...], *, allow: Callable[[tuple[str, ...]], bool] | None = None,
             offset: int = 0, page: int = PAGE, cap: int = LIST_SCAN_CAP) -> dict[str, Any]:
    """One page of a folder: what is in it, what this viewer may see, and what was left out.

    ``allow(parts)`` is the viewer's own rule (D4) and needs no disk access; an entry it refuses is left out of the
    page and of the count, so a listing never advertises what a download would refuse.
    """
    missing = {"exists": False, "entries": [], "next_offset": 0, "truncated": False,
               "truncated_reason": "", "links_skipped": 0, "total": 0}
    try:
        folder = _open_dir(base, parts)
    except DataRefused:
        # A folder that is simply not there answers "not there"; one that is there behind a link is refused. Nothing
        # is created either way.
        if not os.path.lexists(Path(base).joinpath(*parts)):
            return missing
        raise
    if not folder.is_dir():
        return missing
    before = _real(folder)
    rows: list[dict[str, Any]] = []
    links_skipped = 0
    seen = 0
    truncated_reason = ""
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                seen += 1
                if seen > cap:
                    truncated_reason = "folder_too_large"
                    break
                name = entry.name
                if _hidden(Path(name)):
                    continue
                if redirected_entry(entry):
                    links_skipped += 1
                    continue
                is_dir, size, modified = _entry_facts(entry)
                child = (*parts, name)
                if allow is not None and not allow(child):
                    continue
                rows.append({"name": name, "path": "/".join(child), "kind": "dir" if is_dir else "file",
                             "file_kind": "" if is_dir else _kind_for(Path(name)), "size": 0 if is_dir else size,
                             "modified_at": _stamp(modified) if modified else "", "blocked": None})
    except OSError as exc:
        raise DataRefused("unreadable") from exc
    if _real(folder) != before:
        # The folder was swapped while it was being read: this page describes something nobody asked for.
        raise DataRefused("folder_changed")
    rows.sort(key=lambda row: (row["kind"] != "dir", row["name"].casefold()))
    total = len(rows)
    start = max(0, int(offset))
    page_rows = rows[start:start + max(1, int(page))]
    for row in page_rows:
        if row["kind"] == "dir":
            continue
        target = folder / row["name"]
        try:
            info = os.stat(target, follow_symlinks=False)
        except OSError:
            row["blocked"] = "unreadable"
            continue
        # Windows tells a scan no link count; the file system itself does.
        if int(info.st_nlink) != 1:
            row["blocked"] = "multiply_linked"
        row["size"] = int(info.st_size)
        row["modified_at"] = _stamp(float(info.st_mtime))
        if int(info.st_size) > DOWNLOAD_BYTES:
            row["blocked"] = row["blocked"] or "too_large"
    next_offset = start + len(page_rows)
    return {"exists": True, "entries": page_rows, "next_offset": next_offset if next_offset < total else 0,
            "truncated": bool(truncated_reason), "truncated_reason": truncated_reason,
            "links_skipped": links_skipped, "total": total}


def count_dir(base: Path, parts: tuple[str, ...], *, cap: int = COUNT_CAP,
              budget: Budget | None = None) -> dict[str, Any]:
    """How many files and folders are directly inside, counted from the scan alone."""
    try:
        folder = _open_dir(base, parts)
    except DataRefused:
        return {"exists": False, "files": 0, "folders": 0, "truncated": False}
    if not folder.is_dir():
        return {"exists": False, "files": 0, "folders": 0, "truncated": False}
    files = folders = 0
    truncated = False
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if budget is not None and not budget.take():
                    truncated = True
                    break
                if files + folders >= cap:
                    truncated = True
                    break
                if _hidden(Path(entry.name)) or redirected_entry(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    folders += 1
                else:
                    files += 1
    except OSError as exc:
        raise DataRefused("unreadable") from exc
    return {"exists": True, "files": files, "folders": folders, "truncated": truncated}


def exists(base: Path, parts: tuple[str, ...], *, budget: Budget | None = None) -> bool:
    """Whether a folder is there, without following a link into it."""
    if budget is not None:
        budget.take()
    try:
        folder = _open_dir(base, parts)
    except DataRefused:
        return False
    return folder.is_dir()


def _open_checked(root: Path, relative: str) -> tuple[int, Any]:
    """A read-only descriptor for one file below ``root``, checked by the workspace capability (D2)."""
    parts = artifact_path_parts(relative)
    workspace = SecureWorkspace.for_roots(str(root), str(root))
    try:
        entered = workspace.__enter__()
    except Exception as exc:  # noqa: BLE001 - a capability that will not open reads nothing
        raise DataRefused("unreadable") from exc
    try:
        target = entered.resolve("/".join(parts), use_output_root=False)
        return entered.open_read(target), entered
    except WorkspaceBoundaryError as exc:
        entered.__exit__(None, None, None)
        raise DataRefused("multiply_linked" if "ultiply" in str(exc) else "invalid_path") from exc
    except FileNotFoundError as exc:
        entered.__exit__(None, None, None)
        raise DataRefused("not_found") from exc
    except (IsADirectoryError, PermissionError, OSError) as exc:
        entered.__exit__(None, None, None)
        raise DataRefused("unreadable") from exc


def _read_fd(fd: int, limit: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    read = 0
    while read <= limit:
        chunk = os.read(fd, min(65536, limit + 1 - read))
        if not chunk:
            break
        chunks.append(chunk)
        read += len(chunk)
    return b"".join(chunks)


def preview(root: Path, relative: str) -> dict[str, Any]:
    """What the page shows for one file: text up to a window, a small image inline, anything else described.

    Never an absolute path: the console knows the file by the path it asked for.
    """
    fd, workspace = _open_checked(root, relative)
    try:
        info = os.fstat(fd)
        name = Path(relative).name
        kind = _kind_for(Path(name))
        payload: dict[str, Any] = {
            "path": "/".join(artifact_path_parts(relative)), "name": name, "kind": kind,
            "size": int(info.st_size), "modified_at": _stamp(float(info.st_mtime)),
            "content": "", "truncated": False, "data_url": "", "note": "",
        }
        if kind == "text":
            raw = _read_fd(fd, PREVIEW_TEXT_BYTES)
            payload["truncated"] = len(raw) > PREVIEW_TEXT_BYTES
            payload["content"] = raw[:PREVIEW_TEXT_BYTES].decode("utf-8", errors="replace")
        elif kind == "image":
            if int(info.st_size) > PREVIEW_IMAGE_BYTES:
                payload["note"] = "download_it"
            else:
                import base64

                raw = _read_fd(fd, PREVIEW_IMAGE_BYTES)
                suffix = Path(name).suffix.lower().lstrip(".") or "png"
                payload["data_url"] = f"data:image/{'jpeg' if suffix == 'jpg' else suffix};base64," + \
                    base64.b64encode(raw).decode("ascii")
        return payload
    finally:
        os.close(fd)
        workspace.__exit__(None, None, None)


def open_download(root: Path, relative: str) -> tuple[int, Any, dict[str, Any]]:
    """A descriptor to stream one file from, with what the route needs to describe it.

    The caller closes the descriptor and exits the capability (``close_download``).
    """
    fd, workspace = _open_checked(root, relative)
    try:
        info = os.fstat(fd)
        if int(info.st_size) > DOWNLOAD_BYTES:
            raise DataRefused("too_large")
        return fd, workspace, {"name": Path(relative).name, "size": int(info.st_size),
                               "modified_at": _stamp(float(info.st_mtime))}
    except BaseException:
        os.close(fd)
        workspace.__exit__(None, None, None)
        raise


def close_download(fd: int, workspace: Any) -> None:
    try:
        os.close(fd)
    finally:
        workspace.__exit__(None, None, None)


def _clean_write_root(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/").rstrip("/")
    if not text:
        return ""
    try:
        return "/".join(artifact_path_parts(text))
    except ValueError:
        return ""


def _clean_deliverable(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text or len(text) > DELIVERABLE_CHARS or ".." in text.split("/") or order_folder.parse(text).problem:
        return ""
    if not any(char in text for char in "*?["):
        cleaned = _clean_write_root(text)
        # ``dir/`` keeps its slash: it names the files inside the folder, not a file called ``dir`` (C5-1).
        return cleaned + "/" if cleaned and text.endswith("/") else cleaned
    lead = [part for part in text.split("/") if not any(char in part for char in "*?[")]
    if lead and not _clean_write_root("/".join(lead[:-1] if len(lead) == len(text.split("/")) else lead)):
        return ""
    return text


def read_step_table(root: Path, order_id: str) -> dict[str, Any]:
    """The step table frozen when the order was launched: what each step may write and what it delivers.

    Never served as a file; only what it says is used, and only what still reads as a path. ``state`` is ``valid``,
    ``missing``, ``invalid`` or ``too_large`` -- the order-folder rule's answer (``order_folder.read_step_table``, the
    one the owner's receipt reads by too), reached through a folder proven link-free from the project root.
    """
    try:
        folder = _open_dir(root, ("orders", order_id))
    except DataRefused:
        return {"state": "missing", "steps": [], "dropped": 0}
    table = order_folder.read_step_table(folder, order_id)
    if not table.ok:
        return {"state": table.state, "steps": [], "dropped": 0}
    raw_steps = table.data.get("steps")
    steps: list[dict[str, Any]] = []
    dropped = 0
    for item in (raw_steps if isinstance(raw_steps, list) else [])[:STEP_TABLE_STEPS]:
        if not isinstance(item, dict):
            dropped += 1
            continue
        step_id = str(item.get("id", "") or "")[:200]
        if not step_id:
            dropped += 1
            continue
        writes: list[str] = []
        for value in list(item.get("writes", []) or [])[:STEP_TABLE_WRITES]:
            cleaned = _clean_write_root(value)
            if cleaned:
                writes.append(cleaned)
            else:
                dropped += 1
        deliverables: list[str] = []
        for value in list(item.get("deliverables", []) or [])[:STEP_TABLE_DELIVERABLES]:
            cleaned = _clean_deliverable(value)
            if cleaned:
                deliverables.append(cleaned)
            else:
                dropped += 1
        steps.append({
            "id": step_id,
            "name": str(item.get("name", "") or "")[:200],
            # What the table stored is ignored: the segment is what the layout makes of the id today.
            "segment": step_segment(step_id),
            "writes": writes,
            "deliverables": deliverables,
        })
    return {"state": "valid", "steps": steps, "dropped": dropped}


def reveal_exact(folder: Path) -> bool:
    """Open exactly this folder, which the caller has already established is one the owner may be shown.

    Only for a path the product itself recorded -- the folder a project used to live in, which the owner chose.
    It is proven the same way every other opening is (it exists, and nothing on the way is a link), but there is
    no project root to check it against, because it is by definition outside the one the project has now.
    """
    here = Path(folder)
    if not here.is_dir() or redirected(here):
        raise DataRefused("not_found")
    opener = getattr(os, "startfile", None)
    if opener is None:
        raise DataRefused("unsupported")
    try:
        opener(str(here))  # noqa: S606 - the owner asked, on their own machine
    except OSError as exc:
        raise DataRefused("unreadable") from exc
    return True


def reveal_dir(root: Path, parts: tuple[str, ...]) -> bool:
    """Open one folder of the project in the machine's own file manager. Returns whether it was opened.

    The folder is proven first, the same way every listing proves one: no component redirects, and the real path
    below the real root is spelled as it was asked for. So this can only ever open a folder the 资料 entry itself
    would list -- never a link's target, and never a path outside the project.
    """
    folder = _open_dir(root, parts)
    if not folder.is_dir() or redirected(folder):
        raise DataRefused("not_found")
    opener = getattr(os, "startfile", None)
    if opener is None:  # not Windows: the console has no file manager to ask
        raise DataRefused("unsupported")
    try:
        opener(str(folder))  # noqa: S606 - the owner asked, on their own machine
    except OSError as exc:
        raise DataRefused("unreadable") from exc
    return True


def _order_dir(root: Path, order: tuple[str, ...]) -> Path | None:
    """The order's folder, proven link-free from the project root; None when it is not there or not reachable."""
    try:
        folder = _open_dir(root, order)
    except DataRefused:
        return None
    return folder if folder.is_dir() else None


def _deliverable_row(order_dir: Path | None, order: tuple[str, ...], deliverable: str, spend: Budget) -> dict[str, Any]:
    """One declared deliverable's row, by the order-folder rule the gate and the receipt use too (C5-1).

    A plain path says whether its file is there, and where; a pattern (``board/*.png``, ``round1/``) says how many files
    it names -- ``*`` within one name, ``dir/`` the files directly inside, the output tree first and the order folder
    for an older order, hidden files and links never. ``None`` is "the budget ran out before this was known". The
    last disk touch here that did not go through ``confined_dir`` made a row an existence test for any path on the
    machine; the order folder is proven first, and the rule never follows a link below it.
    """
    pattern = order_folder.parse(deliverable)
    starts_in_output = bool(pattern.parts) and os.path.normcase(pattern.parts[0]) == os.path.normcase(OUTPUT)
    where: tuple[str, ...] = () if starts_in_output else (OUTPUT,)
    known = spend.left
    found = None
    if known and order_dir is not None:
        found = order_folder.resolve(order_dir, pattern, cap=GLOB_SCAN_CAP, budget=spend)
        known = not found.truncated
    if pattern.wildcard:
        return {"pattern": deliverable, "matched": (len(found.files) if found is not None else 0) if known else None}
    if found is not None and found.files:
        return {"path": "/".join((*order, *found.where, *found.files[0].split("/"))), "exists": True}
    return {"path": "/".join((*order, *where, *pattern.parts)), "exists": False if known else None}


def order_view(root: Path, order_id: str, table: dict[str, Any], *, budget: Budget | None = None) -> dict[str, Any]:
    """What one order's page shows: its areas, its steps, and how much is in each -- within one budget.

    A count the budget could not finish is ``null`` and ``counts_complete`` is false: an answer that says what it
    does not know, rather than a number that is not true.
    """
    spend = budget or Budget()
    order = ("orders", str(order_id))
    areas: list[dict[str, Any]] = []
    for area, parts in ((OUTPUT, (*order, OUTPUT)), ("inputs", (*order, "inputs")), ("audit", (*order, "audit")),
                        # The order's own download folder is the legacy one; ``downloads`` alone is a step's folder.
                        (DOWNLOADS, (*order, LEGACY_DOWNLOADS)),
                        (DELIVERY_HISTORY, (DELIVERY_HISTORY, str(order_id))),
                        (DELIVERY_EXPORTS, (DELIVERY_EXPORTS, str(order_id)))):
        counted = count_dir(root, parts, budget=spend) if spend.left else {"exists": False, "files": None, "folders": None}
        areas.append({"area": area, "path": "/".join(parts), "exists": bool(counted.get("exists")),
                      "files": counted.get("files"), "folders": counted.get("folders")})
    steps: list[dict[str, Any]] = []
    order_dir = _order_dir(root, order) if spend.left else None
    for step in list(table.get("steps", []) or []):
        segment = str(step.get("segment", "") or "")
        step_row: dict[str, Any] = {"id": step.get("id", ""), "name": step.get("name", ""), "segment": segment,
                                    "folders": [], "deliverables": []}
        for kind in (DOWNLOADS, WORK):
            parts = (*order, STEPS, segment, kind)
            counted = count_dir(root, parts, budget=spend) if spend.left else {"exists": False, "files": None}
            step_row["folders"].append({"kind": kind, "path": "/".join(parts),
                                        "exists": bool(counted.get("exists")), "files": counted.get("files")})
        for declared in list(step.get("writes", []) or []):
            parts = (*order, OUTPUT, *tuple(declared.split("/")))
            counted = count_dir(root, parts, budget=spend) if spend.left else {"exists": False, "files": None}
            step_row["folders"].append({"kind": "writes", "path": "/".join(parts),
                                        "exists": bool(counted.get("exists")), "files": counted.get("files")})
        for deliverable in list(step.get("deliverables", []) or []):
            step_row["deliverables"].append(_deliverable_row(order_dir, order, str(deliverable), spend))
        steps.append(step_row)
    return {"order_id": str(order_id), "areas": areas, "steps": steps,
            "step_table_state": str(table.get("state", "") or ""), "counts_complete": spend.left}


def attachment_page(attachments_root: Path, *, offset: int = 0, page: int = PAGE) -> dict[str, Any]:
    """The project's uploaded attachments, newest first: one folder of sixteen hex characters, one file inside."""
    root = Path(attachments_root)
    if not root.is_dir() or redirected(root):
        return {"exists": False, "entries": [], "next_offset": 0, "truncated": False, "total": 0}
    rows: list[dict[str, Any]] = []
    scanned = 0
    truncated = False
    try:
        with os.scandir(root) as folders:
            for folder in folders:
                scanned += 1
                if scanned > ATTACHMENT_FOLDERS:
                    truncated = True
                    break
                if not _ATTACHMENT_FOLDER.fullmatch(folder.name) or redirected_entry(folder):
                    continue
                if not folder.is_dir(follow_symlinks=False):
                    continue
                files = []
                try:
                    with os.scandir(folder.path) as inner:
                        for item in inner:
                            if _hidden(Path(item.name)) or redirected_entry(item):
                                continue
                            if item.is_file(follow_symlinks=False):
                                files.append(item)
                except OSError:
                    continue
                if not files:
                    continue
                blocked = "unexpected_layout" if len(files) > 1 else None
                first = files[0]
                _is_dir, size, modified = _entry_facts(first)
                rows.append({"attachment_id": folder.name, "name": first.name, "size": size,
                             "modified_at": _stamp(modified) if modified else "",
                             "disk_path": f"{folder.name}/{first.name}", "blocked": blocked})
    except OSError as exc:
        raise DataRefused("unreadable") from exc
    rows.sort(key=lambda row: row["modified_at"], reverse=True)
    start = max(0, int(offset))
    page_rows = rows[start:start + max(1, int(page))]
    for row in page_rows:
        if row["blocked"]:
            continue
        try:
            info = os.stat(root / row["attachment_id"] / row["name"], follow_symlinks=False)
        except OSError:
            row["blocked"] = "unreadable"
            continue
        if int(info.st_nlink) != 1:
            row["blocked"] = "multiply_linked"
        row["size"] = int(info.st_size)
    next_offset = start + len(page_rows)
    return {"exists": True, "entries": page_rows, "next_offset": next_offset if next_offset < len(rows) else 0,
            "truncated": truncated, "total": len(rows)}


_USAGE_BUCKETS = ("orders", DELIVERY_HISTORY, "exports", "project-materials", "other", "internal", "attachments")


def _bucket_of(parts: tuple[str, ...]) -> str:
    head = parts[0] if parts else ""
    if _hidden(Path("/".join(parts))):
        return "internal"
    if head == "orders":
        return "orders"
    if head == DELIVERY_HISTORY:
        return DELIVERY_HISTORY
    if head in {DELIVERY_EXPORTS, PORTFOLIO_EXPORTS}:
        return "exports"
    if head == "project-materials":
        return "project-materials"
    return "other"


# The hidden names this product writes into a project's folder itself. Everything else that happens to be hidden
# -- .git, .venv, node_modules, __pycache__, a dotfile the owner put there -- is not ours, whatever the 资料
# browser hides from view. 「看不见」 and 「可以删」 are different questions, and `_hidden` answers the first.
_OWN_HIDDEN_PREFIX = ".ampuly"


def is_product_area(name: str) -> bool:
    """Whether a name at the top of a project's folder is one this product made, and may therefore remove.

    The named areas come from the same classifier the usage walk buckets by, so those two answers cannot drift.
    Hidden names are deliberately **not** taken from it: `_bucket_of` sends every hidden name to 「internal」
    because the browser does not show them, and reading that as permission to delete would erase the owner's own
    `.git`, `.venv` or `node_modules` out of a folder they chose. Only our own `.ampuly…` names qualify.
    """
    clean = str(name)
    if clean.startswith("."):
        return clean.startswith(_OWN_HIDDEN_PREFIX)
    bucket = _bucket_of((clean,))
    return bucket not in {"other", "internal"}


def project_usage(project_root: Path, attachments_root: Path | None = None, *,
                  zip_roots: Iterable[tuple[Path, str]] = (), budget: Budget | None = None) -> dict[str, Any]:
    """How much space this project takes, by area -- counted, never deleted.

    Sizes are file lengths as the scan reports them, not what the disk allocates; a file with two names is counted
    once per name. Links are never followed and are counted as skipped. The walk stops on its budget and says so.
    """
    spend = budget or Budget(entries=USAGE_ENTRIES, seconds=USAGE_SECONDS)
    buckets = {name: {"bytes": 0, "files": 0} for name in _USAGE_BUCKETS}
    links_skipped = 0
    root = Path(project_root)
    pending: list[tuple[Path, tuple[str, ...], str]] = []
    if root.is_dir() and not redirected(root):
        pending.append((root, (), ""))
    attachments = Path(attachments_root) if attachments_root else None
    if attachments and attachments.is_dir() and not redirected(attachments):
        pending.append((attachments, (), "attachments"))
    while pending:
        if not spend.left:
            break
        current, parts, forced = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if not spend.take():
                        break
                    if redirected_entry(entry):
                        links_skipped += 1
                        continue
                    child = (*parts, entry.name)
                    is_dir, size, _modified = _entry_facts(entry)
                    if is_dir:
                        pending.append((Path(entry.path), child, forced))
                        continue
                    bucket = forced or _bucket_of(child)
                    buckets[bucket]["bytes"] += size
                    buckets[bucket]["files"] += 1
        except OSError:
            continue
    archives = {"count": 0, "bytes": 0}
    for folder, project_id in zip_roots:
        pattern = re.compile(rf"^{re.escape(str(project_id))}-\d{{8}}T\d{{12}}\.zip$")
        directory = Path(folder)
        if not directory.is_dir() or redirected(directory):
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not spend.take():
                        break
                    if redirected_entry(entry) or not pattern.fullmatch(entry.name):
                        continue
                    _is_dir, size, _modified = _entry_facts(entry)
                    archives["count"] += 1
                    archives["bytes"] += size
        except OSError:
            continue
    total = sum(bucket["bytes"] for bucket in buckets.values()) + archives["bytes"]
    return {"buckets": buckets, "archives": archives, "total_bytes": total,
            "links_skipped": links_skipped, "complete": spend.left, "entries_seen": spend.spent}
