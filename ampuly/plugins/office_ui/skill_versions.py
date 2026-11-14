"""Which kept skill bodies could be cleaned up: a list, never a deletion (SM-1, owner ruling ).

Every launch keeps the body of each skill it sends an order with under ``skills/.versions/<skill>/<fingerprint>/``
(candidate BA, ``workflow_packages._keep_skill_version``): write-once, and nothing removes one, so the folder only
grows. The rule the owner chose for cleaning it up keeps a version when

1. an order that has not ended was sent with it -- in any project, the fingerprints its launch froze on the task it
   launched (``workflow_contract.skill_sha256``, read with ``skill_library.frozen_skill_sha256``); or
2. it is one of the newest three of its skill, newest by the last order sent with it, else by the date of its files.

Every other version is "可以清理". This module only reads -- the kept folders, the business book and each project's
runtime store, all opened read-only -- and lists them with their size and when an order last used them. Removing
any of them is a confirmation moment of its own; nothing here does it.

It fails closed: when an order that has not ended may be running on a version this cannot see (its project's runtime
store is missing or unreadable, its launched task is not there, it is being sent right now, or the book could not be
read to the end), nothing is listed as removable, and the report says which orders and why.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from ampuly.core.sqlite_open import opened
from ampuly.database.store import default_runtime_db_path
from ampuly.layer5_memory.skill_library import (
    SKILL_VERSIONS_DIRNAME,
    frozen_skill_sha256,
    skill_version_dir,
)
from ampuly.plugins.office_ui.business_store import (
    ENDED_ORDER_STATUSES,
    ORDER_LIST_HARD_CAP,
    BusinessStore,
    WorkOrder,
    order_cursor,
    parse_order_cursor,
)
from ampuly.project_id import is_valid_project_id

# The newest versions of each skill kept whatever else is true (owner ruling ).
KEEP_NEWEST = 3

# Why a version is kept. A removable version has ``kept_because == ""``.
KEPT_IN_USE = "in_use"  # an order that has not ended was sent with it
KEPT_NEWEST = "newest"  # one of the newest KEEP_NEWEST of its skill
KEPT_UNRESOLVED = "unresolved"  # nothing is removable while an order that has not ended cannot be read

# Orders that have not ended and may be running now: each one's launched task must be read to know what it runs on.
_OUT_STATUSES = frozenset({"launching", "launched", "producing", "delivering"})
_STATUS_WORDS = {
    "collecting": "还在收集需求",
    "pending_review": "等审核",
    "launching": "正在发出",
    "launched": "已发出",
    "producing": "正在制作",
    "delivering": "正在交付",
}
_SKILL_MD_READ_CAP = 64 * 1024
_TASK_LOOKUP_CHUNK = 400


@dataclass(frozen=True)
class KeptSkillVersion:
    """One kept body: ``skills/.versions/<skill>/<fingerprint>/``."""

    skill: str  # the folder the launch installed it in (``skills/<skill>``), which is also the name orders pin
    fingerprint: str  # SKILL.md's sha256 as a launch freezes it (``skill_md_sha256``)
    path: Path
    size_bytes: int
    last_used: str  # when the newest order sent with it was launched (ISO), "" when no order on record used it
    files_at: str  # the kept folder's date (ISO); a copy keeps the date of the files it was made from
    version: str = ""  # the version its SKILL.md declares (``metadata.version``), "" when it declares none
    kept_because: str = ""  # "" = removable; else KEPT_IN_USE / KEPT_NEWEST / KEPT_UNRESOLVED


@dataclass(frozen=True)
class SkillVersionsReport:
    removable: list[KeptSkillVersion] = field(default_factory=list)
    kept: list[KeptSkillVersion] = field(default_factory=list)
    # Owner words for each order that has not ended whose frozen versions could not be read. Not empty = nothing
    # is removable this time.
    unresolved: list[str] = field(default_factory=list)

    @property
    def removable_bytes(self) -> int:
        return sum(item.size_bytes for item in self.removable)


# ── the business book, read-only ───────────────────────────────────────────────────────────────────────────────

class ReadOnlyOrders:
    """The work orders of ``business.db`` read with ``mode=ro``: what the report needs from the business store.

    ``BusinessStore`` opens its file for writing and brings its schema up to date on construction, and makes the file
    when there is none; a report that must touch nothing reads the book this way instead. No ``business.db`` = no
    orders. Only whole-book pages: the report reads every project.
    """

    def __init__(self, ampuly_home: Path | str) -> None:
        self.path = Path(ampuly_home) / "business.db"

    def list_orders_page(
        self, *, project_id: str | None = None, limit: int = 200, before: str | None = None,
    ) -> tuple[list[WorkOrder], str]:
        if project_id is not None:
            raise ValueError("ReadOnlyOrders reads the whole book only")
        if not self.path.is_file():
            return [], ""
        size = max(1, min(int(limit), ORDER_LIST_HARD_CAP))
        where, values = "", []
        if before is not None:
            created_at, order_id = parse_order_cursor(before)
            where = " WHERE (created_at < ? OR (created_at = ? AND order_id < ?))"
            values = [created_at, created_at, order_id]
        with opened(self.path, read_only=True, row_factory=sqlite3.Row) as conn:
            rows = conn.execute(
                f"SELECT * FROM work_orders{where} ORDER BY created_at DESC, order_id DESC LIMIT ?",
                (*values, size + 1),
            ).fetchall()
        orders = [BusinessStore._row_to_order(row) for row in rows]
        page = orders[:size]
        return page, (order_cursor(page[-1]) if len(orders) > size and page else "")


def _all_orders(business: Any) -> tuple[list[Any], bool]:
    """Every order in the book, page by page, and whether the walk reached the end."""
    orders: list[Any] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        page, following = business.list_orders_page(limit=ORDER_LIST_HARD_CAP, before=cursor)
        orders.extend(page or [])
        if not following or not page:
            return orders, True
        if following in seen:  # a cursor that does not move on: the rest of the book cannot be reached
            return orders, False
        seen.add(following)
        cursor = following


# ── what each order was sent with ──────────────────────────────────────────────────────────────────────────────

def _runtime_db(ampuly_home: Path, project_id: str) -> Path | None:
    """A project's runtime store, where its engine opens it; None for a project id no engine would open."""
    if project_id == "default":
        return default_runtime_db_path(ampuly_home)
    if not is_valid_project_id(project_id):
        return None
    return ampuly_home / "projects" / project_id / "tasks.db"


def _launched_tasks(db: Path | None, task_ids: list[str]) -> dict[str, tuple[str, str]] | None:
    """``task id -> (created_at, metadata JSON)`` of the ones found, read-only; None when the store cannot be read."""
    if db is None or not db.is_file():
        return None
    found: dict[str, tuple[str, str]] = {}
    try:
        with opened(db, read_only=True, timeout=2.0) as conn:
            for start in range(0, len(task_ids), _TASK_LOOKUP_CHUNK):
                chunk = task_ids[start:start + _TASK_LOOKUP_CHUNK]
                marks = ",".join("?" for _ in chunk)
                for row in conn.execute(
                    f"SELECT id, created_at, metadata FROM tasks WHERE id IN ({marks})", tuple(chunk),
                ).fetchall():
                    found[str(row[0])] = (str(row[1] or ""), str(row[2] or ""))
    except sqlite3.Error:
        return None
    return found


def _order_words(order: Any) -> str:
    """An order in the owner's words: whose it is, the day it was placed and where it stands. No ids."""
    names = " · ".join(
        name for name in (str(getattr(order, "customer_name", "") or "").strip(),
                          str(getattr(order, "brand_name", "") or "").strip()) if name
    )
    placed = _day(str(getattr(order, "created_at", "") or ""))
    status = _STATUS_WORDS.get(str(getattr(order, "status", "") or ""), "还没结束")
    who = f"「{names}」的工单" if names else "一张工单"
    return f"{who}（{placed + '下的单，' if placed else ''}{status}）"


def _frozen_uses(ampuly_home: Path, orders: list[Any]) -> tuple[set[str], dict[str, str], list[str]]:
    """What the book's orders were sent with.

    Returns the fingerprints orders that have not ended were sent with, the newest launch time of every fingerprint
    any order on record was sent with, and owner words for each order that has not ended whose fingerprints could not
    be read.
    """
    in_use: set[str] = set()
    last_used: dict[str, str] = {}
    unresolved: list[str] = []
    by_project: dict[str, list[Any]] = {}
    for order in orders:
        status = str(getattr(order, "status", "") or "")
        task_id = str(getattr(order, "launched_task_id", "") or "").strip()
        if not task_id:
            # One in intake or review has pinned nothing yet: its launch keeps the version it sends it with.
            if status == "launching":
                unresolved.append(f"{_order_words(order)}：这张单此刻正在发出，还不知道它会用哪一版")
            elif status in _OUT_STATUSES:
                unresolved.append(f"{_order_words(order)}：记录里没有它发出的那次运行")
            continue
        project = str(getattr(order, "project_id", "") or "default").strip() or "default"
        by_project.setdefault(project, []).append(order)

    for project, project_orders in sorted(by_project.items()):
        ids = sorted({str(order.launched_task_id).strip() for order in project_orders})
        rows = _launched_tasks(_runtime_db(ampuly_home, project), ids)
        for order in project_orders:
            status = str(getattr(order, "status", "") or "")
            live = status not in ENDED_ORDER_STATUSES
            row = None if rows is None else rows.get(str(order.launched_task_id).strip())
            metadata: Any = None
            if row is not None:
                try:
                    metadata = json.loads(row[1]) if row[1] else {}
                except ValueError:
                    metadata = None
            if metadata is None:
                # Only an order out running must have its run read: one back in intake or review runs nothing now.
                if status in _OUT_STATUSES:
                    where = (
                        "它所在项目的运行记录读不到" if rows is None
                        else "它所在项目的运行记录里找不到这次发单" if row is None
                        else "它发单时的记录读不懂"
                    )
                    unresolved.append(f"{_order_words(order)}：{where}")
                continue
            created_at = row[0] if row is not None else ""
            for fingerprint in frozen_skill_sha256(metadata).values():
                if live:
                    in_use.add(fingerprint)
                if created_at and _moment(created_at) > _moment(last_used.get(fingerprint, "")):
                    last_used[fingerprint] = created_at
    return in_use, last_used, unresolved


# ── the kept folders ───────────────────────────────────────────────────────────────────────────────────────────

def _is_link(path: Path | os.DirEntry[str]) -> bool:
    """A symlink or a Windows junction: never followed, never counted."""
    target = Path(path.path) if isinstance(path, os.DirEntry) else Path(path)
    try:
        return target.is_symlink() or bool(getattr(target, "is_junction", lambda: False)())
    except OSError:
        return True


def _folder_size(root: Path) -> int:
    """Bytes of the files under ``root``, links neither followed nor counted."""
    total = 0
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if _is_link(entry):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def _declared_version(folder: Path) -> str:
    """``metadata.version`` from the kept SKILL.md's frontmatter, or ""."""
    try:
        with (folder / "SKILL.md").open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(_SKILL_MD_READ_CAP)
    except OSError:
        return ""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return ""
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
        front = yaml.safe_load("\n".join(lines[1:end])) or {}
    except (StopIteration, yaml.YAMLError):
        return ""
    metadata = front.get("metadata") if isinstance(front, dict) else None
    version = metadata.get("version") if isinstance(metadata, dict) else None
    return str(version).strip() if version not in (None, "") else ""


def _kept_folders(ampuly_home: Path) -> list[tuple[str, str, Path]]:
    """``(skill, fingerprint, folder)`` of every kept body; anything else under ``.versions`` is not a version."""
    skills_dir = ampuly_home / "skills"
    root = skills_dir / SKILL_VERSIONS_DIRNAME
    if not root.is_dir() or _is_link(root):
        return []
    found: list[tuple[str, str, Path]] = []
    try:
        skill_entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        return []
    for skill_entry in skill_entries:
        if _is_link(skill_entry) or not skill_entry.is_dir(follow_symlinks=False):
            continue
        try:
            version_entries = sorted(os.scandir(skill_entry.path), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in version_entries:
            if _is_link(entry) or not entry.is_dir(follow_symlinks=False):
                continue
            # Only a folder named the way a launch names one: a half-made copy (".staging-...") is not a version.
            expected = skill_version_dir(skills_dir, skill_entry.name, entry.name)
            if expected is None or expected.name != entry.name:
                continue
            found.append((skill_entry.name, entry.name, Path(entry.path)))
    return found


# ── the rule ───────────────────────────────────────────────────────────────────────────────────────────────────

def _moment(value: str) -> datetime:
    """An ISO time as a local, naive datetime for ordering; the earliest possible one when absent or unreadable."""
    try:
        moment = datetime.fromisoformat(str(value or "").strip())
    except ValueError:
        return datetime.min
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def _recency(item: KeptSkillVersion) -> datetime:
    return max(_moment(item.last_used), _moment(item.files_at))


def skill_versions_report(ampuly_home: Path | str, business: Any) -> SkillVersionsReport:
    """Sort every kept skill body into removable and kept by the owner's rule. Reads only.

    ``business`` is the business store, or anything with its ``list_orders_page`` (``ReadOnlyOrders``).
    """
    home = Path(ampuly_home)
    orders, complete = _all_orders(business)
    in_use, last_used, unresolved = _frozen_uses(home, orders)
    if not complete:
        unresolved.insert(0, "工单列表没能从头读到尾，可能漏看了还没结束的工单")

    by_skill: dict[str, list[KeptSkillVersion]] = {}
    for skill, fingerprint, folder in _kept_folders(home):
        try:
            files_at = datetime.fromtimestamp(folder.stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            files_at = ""
        by_skill.setdefault(skill, []).append(KeptSkillVersion(
            skill=skill,
            fingerprint=fingerprint,
            path=folder,
            size_bytes=_folder_size(folder),
            last_used=last_used.get(fingerprint, ""),
            files_at=files_at,
            version=_declared_version(folder),
        ))

    removable: list[KeptSkillVersion] = []
    kept: list[KeptSkillVersion] = []
    for skill in sorted(by_skill):
        ranked = sorted(by_skill[skill], key=lambda item: (_recency(item), item.fingerprint), reverse=True)
        for rank, item in enumerate(ranked):
            if item.fingerprint in in_use:
                kept.append(replace(item, kept_because=KEPT_IN_USE))
            elif rank < KEEP_NEWEST:
                kept.append(replace(item, kept_because=KEPT_NEWEST))
            elif unresolved:
                kept.append(replace(item, kept_because=KEPT_UNRESOLVED))
            else:
                removable.append(item)
    return SkillVersionsReport(removable=removable, kept=kept, unresolved=unresolved)


def removable_versions(ampuly_home: Path | str, business: Any) -> list[KeptSkillVersion]:
    """The kept skill bodies the owner's rule would let go, newest first within each skill. Removes nothing."""
    return skill_versions_report(ampuly_home, business).removable


# ── in the owner's words ───────────────────────────────────────────────────────────────────────────────────────

def _day(value: str) -> str:
    moment = _moment(value)
    return "" if moment == datetime.min else f"{moment.month}月{moment.day}日"


def _when(value: str) -> str:
    moment = _moment(value)
    return "" if moment == datetime.min else moment.strftime("%Y-%m-%d %H:%M")


def size_words(size: int) -> str:
    if size < 1024:
        return f"{size} 字节"
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} 字节"


def report_text(report: SkillVersionsReport) -> str:
    """The list in plain Chinese, with the total size. No fingerprints or other ids: each version is named by its
    skill, the version its file declares, when an order last used it and the date of its files."""
    lines = [
        "技能旧版本清理清单（只列出来，什么都没删）",
        f"规则：还没结束的工单发单时用的那一版留着；每个技能最新的 {KEEP_NEWEST} 版留着；其余的算「可以清理」。",
        "",
    ]
    if report.unresolved:
        lines.append(f"有 {len(report.unresolved)} 处读不清还没结束的工单用的是哪一版技能，所以这次一版都不算可以清理：")
        lines.extend(f"- {item}" for item in report.unresolved)
        lines.append("等这些工单结束，或者查清楚之后再看一次。")
    elif report.removable:
        lines.append(f"可以清理的有 {len(report.removable)} 版，合计 {size_words(report.removable_bytes)}：")
        for item in report.removable:
            label = f"「{item.skill}」" + (f"{item.version} 版" if item.version else "的一版")
            used = f"最近一次发单用到是 {_when(item.last_used)}" if item.last_used else "没有记录到哪张工单用过"
            dated = f"；文件日期 {_when(item.files_at)}" if item.files_at else ""
            lines.append(f"- {label}：{used}{dated}；占 {size_words(item.size_bytes)}")
    else:
        lines.append("现在没有可以清理的旧版本。")
    in_use = sum(1 for item in report.kept if item.kept_because == KEPT_IN_USE)
    kept_bytes = sum(item.size_bytes for item in report.kept)
    lines.append("")
    lines.append(
        f"留着的有 {len(report.kept)} 版，合计 {size_words(kept_bytes)}"
        + (f"（其中 {in_use} 版还有没结束的工单在用）" if in_use else "")
        + "。"
    )
    lines.append("真要删掉哪些，需要你另外确认；这份清单不会删任何东西。")
    return "\n".join(lines)
