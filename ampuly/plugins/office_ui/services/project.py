"""Project lifecycle service shared by Office UI and CLI."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from ampuly.database.store import default_runtime_db_path
from ampuly.layer5_memory.markdown_memory import MarkdownMemoryStore
from ampuly.plugins.office_ui.business_store import BusinessStore
from ampuly.plugins.office_ui.project_alias import load_aliases, register_alias, resolve_project_alias
from ampuly.plugins.office_ui.snapshot_builder import build_collab_sync, build_project_index_sync, build_snapshot
from ampuly.project_id import project_id_policy

from .context import OfficeServiceContext
from .models import ServiceEvent, ServiceError, ServiceResult


# Where a project's business records travel inside an export or backup archive.
BUSINESS_RECORDS_MEMBER = "business/records.json"
# 数据与文件 P3: the members that carry what does not live under the project's own folders. Each is one JSON
# document, digested like a file, so the archive's own verification covers it.
# 数据与文件 P3/P4：这个项目的运行库（任务、会话、断点）。
#
# P3 时它只为默认项目而设，因为默认项目的库孤零零躺在总目录根上叫 `global.db`，谁都没带过它；别的项目
# 的库在项目树里，「已经带走了」。但那是**按普通文件拷走的**——引擎在跑的时候，拷到的可能是写了一半的
# 那一份，而旁边的 -wal 和它对不上。恢复出来是一份打得开、内容却不自洽的库。
#
# P4 起每个项目都走这个成员：用 SQLite 在线备份接口取一致快照，项目树里那几个活文件不再按文件拷。
RUNTIME_DB_MEMBER = "runtime/tasks.db"
# 项目文件夹里那几个「活」文件：它们由上面那个成员以快照形式带走，不跟着项目树按文件拷。
LIVE_RUNTIME_FILES = ("tasks.db", "tasks.db-wal", "tasks.db-shm")
BUDGETS_MEMBER = "business/budgets.json"
HISTORY_MEMBER = "business/history.json"
CHAT_MEMBER = "chat/messages.json"
ALLOWLIST_MEMBER = "config/approval_allowlist.json"
JSON_MEMBERS = (BUSINESS_RECORDS_MEMBER, BUDGETS_MEMBER, HISTORY_MEMBER, CHAT_MEMBER, ALLOWLIST_MEMBER)
# 不在任何目录前缀之下的成员，打包与还原共用这一份。两边各列一份的话，对不上的那次就是还原不了的那次。
FLAT_MEMBERS = (*JSON_MEMBERS, RUNTIME_DB_MEMBER)
# 恢复时会放回去的成员。
RESTORED_MEMBERS = (BUSINESS_RECORDS_MEMBER, CHAT_MEMBER, RUNTIME_DB_MEMBER)
# 备份里带着、恢复时**故意**不放回去的成员，以及为什么不放。
#
# 「忘了带」和「故意不带」看起来一模一样——这句话在打包那一侧已经立过一次规矩，这里是它在还原这一侧的
# 同一句话。一张结构性测试盯着：每个成员要么在 RESTORED_MEMBERS 里，要么在这里带着理由，不许有第三种。
# 前三条的共同理由值得写清楚：销毁一个项目**并没有**删掉预算和生命周期（见 purge_project_operational_data
# 的「retaining lifecycle audit」），它们现在还在业务库里。再放一遍不是补齐，是把同一段历史记两遍。
NOT_RESTORED: tuple[tuple[str, str], ...] = (
    (BUDGETS_MEMBER, "预算、预留和暂缓的拦截在销毁时没有被删掉，现在还在业务库里；再放一遍会变成两份"),
    (HISTORY_MEMBER, "项目的生命周期和观察审计同样留着（那是留痕）；再放一遍会把同一段历史记两遍"),
    (ALLOWLIST_MEMBER, "这是授权，不是数据。恢复一个项目不该顺手把它当初能访问的账号和网站重新放开——要你重新点一次"),
)


def _sqlite_snapshot(source: Path, target: Path) -> bool:
    """A consistent copy of a live SQLite database, or False when there is nothing to copy.

    Uses SQLite's own backup API rather than copying the file: the console keeps the database open, and a plain
    copy taken while it is being written produces a file that digests differently three lines later -- which is
    how a backup used to fail with 「content digest mismatch」 and delete its own output. The API takes a
    consistent snapshot without locking the writer out.
    """
    if not Path(source).is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    # 显式 close，不用 `with`：sqlite3 的上下文管理器**只提交、不关闭**。漏下的句柄在 Windows 上会把文件
    # 锁住，于是紧接着删除这个项目时，它的运行库删不掉——而删除那一步用的是 `ignore_errors=True`，失败是
    # 不出声的。备份刚好总是紧挨着删除（删之前必须有一份备份是这一期立的规矩），所以这不是理论问题。
    live = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(target)
        try:
            live.backup(copy)
        finally:
            copy.close()
    finally:
        live.close()
    return True


def _staging_io_path(path: Path) -> str:
    """Represent a verified local staging root for Windows long-path file I/O.

    Keep canonical project/security paths unchanged. The temporary restore
    prefix can push otherwise valid asset names past MAX_PATH during extraction.
    """
    raw = str(path)
    if os.name == "nt" and path.is_absolute() and not raw.startswith("\\\\"):
        return "\\\\?\\" + raw
    return raw


def backup_list_root(home: Path) -> Path:
    """备份放在哪。和备份列表用同一个答案，不各写一遍。"""
    from ampuly.plugins.office_ui import backup_list

    return backup_list.backups_root(home)


def _json_bytes(payload: Any) -> bytes:
    """One JSON member's bytes. Sorted and separator-fixed so the same content always digests the same."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class ProjectService:
    def __init__(self, context: OfficeServiceContext) -> None:
        self.context = context

    # ── A folder the owner named ─────────────────────────────────────────────────────────────────────────────────

    def _pinned_folder(self, project_id: str) -> Path | None:
        """The folder this project is pinned to, whoever chose it, or ``None`` when nothing is recorded."""
        from ampuly.core import project_paths

        return project_paths.mapping(self.context.ampuly_home).pinned(str(project_id))

    def _owner_chose(self, project_id: str) -> Path | None:
        """The folder the owner named, or ``None`` -- which includes a pin that merely repeats our own answer.

        Setting a new default root pins **every** existing project in place, at ``<old root>/<pid>``. Those pins
        are not the owner naming a folder; they are the product recording where it already put things. Treating
        them as chosen would flip every project onto the careful-deletion path for a folder inside our own home,
        leave our files standing and then report them to the owner as theirs.
        """
        from ampuly.core import project_paths

        pinned = self._pinned_folder(project_id)
        if pinned is None:
            return None
        if project_paths.product_owned(self.context.ampuly_home, str(project_id), pinned):
            return None
        return pinned

    def _repoint(self, moves: dict[str, str], *, drop: tuple[str, ...] = (), actor: str = "owner") -> None:
        """Rewrite the mapping: ``moves`` carries a pin to a new project id, ``drop`` forgets one.

        A destroyed project must not leave its folder pinned -- a project made with the same name a week later
        would otherwise inherit a folder the owner deleted, or a path that no longer exists.
        """
        from ampuly.core import project_paths

        home = self.context.ampuly_home
        found = project_paths.mapping(home)
        projects = dict(found.projects)
        legacy = dict(found.legacy)
        for old, new in moves.items():
            path = projects.pop(str(old), None)
            if path is not None and not project_paths.product_owned(home, str(old), path):
                projects[str(new)] = path
            # A pin that only repeated our own answer -- <root>/<old id> -- says nothing about the new name, and
            # under it the loader would refuse the path outright. Dropping it lets the resolver compute the new
            # folder, which is what the pin was saying in the first place.
            was = legacy.pop(str(old), None)
            if was is not None:
                legacy[str(new)] = was
        for pid in drop:
            projects.pop(str(pid), None)
            legacy.pop(str(pid), None)
        if projects == found.projects and legacy == found.legacy:
            return
        after = project_paths.save(home, default_root=found.default_root, projects=projects, legacy=legacy,
                                   actor=actor)
        # save() answers with what a fresh read makes of the file, and a read re-checks every entry. A pin that
        # only passed before because it was exactly <root>/<old id> no longer matches under the new id, so it is
        # dropped -- and a rename that did not look would report success while the project's files became
        # unreachable. Put back what was there and let the caller refuse.
        lost = [pid for pid in (*moves.values(), ) if pid in projects and after.pinned(pid) is None]
        if lost:
            project_paths.save(home, default_root=found.default_root, projects=dict(found.projects),
                               legacy=dict(found.legacy), actor=actor)
            raise ServiceError("pin_rejected",
                               "The folder cannot be recorded under that name; nothing was changed",
                               {"projects": lost})

    def _remove_workplace(self, project_id: str, *, ours_may_swallow: bool) -> dict[str, Any] | None:
        """Erase the project's files, and answer with what was left standing.

        For a folder the product made -- ``<root>/<pid>`` -- the whole thing goes, as it always has, and
        ``ours_may_swallow`` is each caller's own long-standing answer about a file it could not remove. That
        parameter exists so this stays byte-identical to what each caller did before: ``delete`` swallowed,
        ``destroy`` did not, and changing either would be a new failure mode on the path almost every project
        takes.

        For a folder the **owner** named, only the areas this product created are removed and then the folder
        itself is asked to go, which it does only when it is empty. Anything else they keep in there is theirs:
        the reply names it so the console can say so, rather than erasing it for sharing a folder with us. And
        nothing is ever swallowed there -- a file we could not remove from a folder on the owner's own disk is
        something they have to be told about, not something to quietly count as done.
        """
        workplace = self.context.project_workplace(project_id)
        if self._owner_chose(project_id) is None:
            if workplace.is_dir():
                shutil.rmtree(str(workplace), ignore_errors=ours_may_swallow)
            return None
        if not workplace.is_dir():
            return None
        from ampuly.plugins.office_ui.data_browser import is_product_area

        try:
            here = list(workplace.iterdir())
        except OSError:
            here = []
        for target in here:
            # Asking the same classifier the usage walk uses, rather than keeping a second list of area names:
            # a list would drift, and a folder of ours left behind would be reported to the owner as theirs.
            if not is_product_area(target.name):
                continue
            if target.is_dir():
                shutil.rmtree(str(target), ignore_errors=False)
            else:
                target.unlink()
        try:
            workplace.rmdir()
        except OSError:
            pass
        if not workplace.exists():
            return None
        try:
            left = len(list(workplace.iterdir()))
        except OSError:
            left = 0
        return {"path": str(workplace), "entries": left}

    @staticmethod
    def _same_folder(left: Path, right: Path) -> bool:
        import os

        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))

    @staticmethod
    def _quote_sql_identifier(name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    @classmethod
    def _rewrite_project_id_in_sqlite(cls, db_path: Path, old_project_id: str, new_project_id: str) -> dict[str, int]:
        if not db_path.exists():
            return {}
        counts: dict[str, int] = {}
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            for (table_name,) in rows:
                table = str(table_name or "")
                if not table or table.startswith("sqlite_"):
                    continue
                quoted = cls._quote_sql_identifier(table)
                columns = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
                if not any(str(col[1]) == "project_id" for col in columns):
                    continue
                cursor = conn.execute(
                    f"UPDATE {quoted} SET project_id = ? WHERE project_id = ?",
                    (new_project_id, old_project_id),
                )
                counts[table] = int(cursor.rowcount or 0)
            conn.commit()
        finally:
            conn.close()
        return counts

    async def _close_project_engine_store(self, project_id: str) -> None:
        root = self.context.root_engine
        candidates: list[Any] = []
        active = self.context.engine
        if self.context.normalize_project_id(getattr(active, "project_id", None)) == project_id:
            candidates.append(active)
        if self.context.normalize_project_id(getattr(root, "project_id", None)) == project_id:
            candidates.append(root)
        delegates = getattr(root, "_project_engine_delegates", None)
        if isinstance(delegates, dict):
            delegate = delegates.pop(project_id, None)
            if delegate is not None:
                candidates.append(delegate)
        seen: set[int] = set()
        for engine in candidates:
            marker = id(engine)
            if marker in seen:
                continue
            seen.add(marker)
            store = getattr(engine, "store", None)
            close = getattr(store, "close", None)
            if callable(close):
                try:
                    maybe = close()
                    if asyncio.iscoroutine(maybe):
                        await maybe
                except Exception:
                    logger.opt(exception=True).debug(f"Failed to close project store for {project_id}")

    def _project_list_payload(self, active_project_id: str) -> dict[str, Any]:
        entries = self.context.list_project_entries()
        ampuly_home = getattr(self.context, "ampuly_home", None)
        customers: list[str] = []
        if ampuly_home is not None:
            business = BusinessStore(ampuly_home)
            display_names = business.project_display_names()
            for entry in entries:
                shown = display_names.get(str(entry.get("id", ""))) or entry.get("name") or entry.get("id")
                entry["name"] = shown
                entry["display_name"] = shown
                lifecycle = business.project_lifecycle(str(entry.get("id", "")))
                entry["lifecycle_status"] = lifecycle.status
                if lifecycle.updated_at:
                    entry["lifecycle_updated_at"] = lifecycle.updated_at
            # 数据与文件 P4：已经有过的客户。绑定那个框一直是个自由文本框，打错一个字母就静默绑到一个
            # 不存在的客户上；在这之前控制台里**没有任何地方**能回答「我有哪些客户」。名单只做提示，不
            # 拦人——建表之前绑好的那些项目本来就不在名单里，拦了就是把真实存在的绑定判成非法。
            customers = [str(row["customer_id"]) for row in business.list_customers()]
        return {
            "projects": entries,
            "customers": customers,
            "active_project_id": self.context.normalize_project_id(active_project_id),
            "project_id_policy": project_id_policy(),
        }

    def _lifecycle(self, project_id: str):
        return BusinessStore(self.context.ampuly_home).project_lifecycle(project_id)

    @staticmethod
    def _actor(actor: str | None) -> str:
        return str(actor or "owner").strip() or "owner"

    async def list(self, *, active_project_id: str | None = None) -> ServiceResult:
        active = active_project_id or self.context.active_engine_project_id()
        return ServiceResult(self._project_list_payload(active))

    async def lifecycle(self, project_id: str, *, limit: int = 100) -> ServiceResult:
        clean = str(project_id or "").strip()
        if not clean or not self.context.is_safe_project_id(clean):
            raise ServiceError("invalid_project_id", "Invalid project_id")
        store = BusinessStore(self.context.ampuly_home)
        record = store.project_lifecycle(clean)
        return ServiceResult({
            "project_id": clean,
            "lifecycle": record.to_payload(),
            "events": store.project_lifecycle_events(clean, limit=limit),
        })

    async def create(self, project_id: str, *, active_project_id: str | None = None) -> ServiceResult:
        project_id = str(project_id or "").strip()
        if not project_id:
            raise ServiceError("missing_project_id", "Missing project_id")
        if not self.context.is_safe_project_id(project_id):
            raise ServiceError("invalid_project_id", "Invalid project_id (use alphanumeric, hyphens, underscores)")

        if resolve_project_alias(self.context.ampuly_home, project_id) != project_id:
            raise ServiceError("project_exists", f"Project '{project_id}' is the display name of an existing project",
                               {"project_id": project_id})
        projects_dir = self.context.project_dir(project_id)
        memory_store = MarkdownMemoryStore(Path(self.context.root_engine.ampuly_home))
        memory_path = memory_store.memory_path(project_id)
        workplace = self.context.project_workplace(project_id)
        if projects_dir.exists() or memory_path.exists() or workplace.exists():
            raise ServiceError("project_exists", f"Project '{project_id}' already exists")

        projects_dir.mkdir(parents=True, exist_ok=False)
        workplace.mkdir(parents=True, exist_ok=False)
        memory_store.ensure_memory_file(project_id, f"# Project Memory ({project_id})")
        business = BusinessStore(self.context.ampuly_home)
        # 数据与文件 P4：新建的项目登记进档案表。它回答生命周期表答不了的那个问题——这个项目到底存不存在
        # （生命周期表对一个从没存在过的名字照样答 active）。建表之前就有的那些项目档案里没有行，那是「还
        # 没登记」，不是「不存在」；所以现在**没有任何地方**拿这个答案去拦人。
        business.record_project(project_id, actor="owner")
        business.record_project_lifecycle(
            project_id,
            "active",
            actor="owner",
            reason="project_created",
            metadata={"source": "office_ui"},
            event_action="project_created",
        )
        active = active_project_id or self.context.active_engine_project_id()
        return ServiceResult({
            **self._project_list_payload(active),
            "action": "create_project",
            "project_id": project_id,
        })

    def _rename_would_orphan(self, project_id: str) -> dict[str, int]:
        """改名会把哪些记录丢在旧名字底下。空表示不会丢。

        改名只重写了项目自己那个任务库，**业务库从头到尾没被打开过**，而工单是按记录里的项目字段认的。所以
        改完名，订单、生命周期、预算、流程采用全部留在旧名下——这是今天就存在的静默损坏，不是将来的风险。
        """
        store = BusinessStore(self.context.ampuly_home)
        history = dict(store.export_project_history(project_id)["counts"])
        pairs = (
            ("业务记录", store.export_project_records(project_id)["counts"]),
            ("预算", store.export_project_budgets(project_id)["counts"]),
            # 生命周期那两张表**不算**：每个新建的项目都有，拿它拦住改名等于所有改名都拒。它们留在旧名下
            # 是小得多的损失（新名字重新开始记），值得在返回里说一句，但不该挡住一次正常的改名。
            ("浏览器观测记录", {"observation_audit": history.get("observation_audit", 0)}),
        )
        counts: dict[str, int] = {}
        for name, rows in pairs:
            total = sum(int(value) for value in dict(rows).values())
            if total:
                counts[name] = total
        return counts

    def _adopted_employee_assets(self, project_id: str) -> dict[str, int]:
        """Instances and bindings whose identity is this project id.

        Their files pin ``project_id`` inside a hash chain. Moving the directory
        makes those reads fail. A display-name change does not.
        """
        root = self.context.project_dir(project_id)
        instance_dir = root / "employee_instances"
        instances = len(list(instance_dir.glob("ei-*.json"))) if instance_dir.is_dir() else 0
        bindings = 0
        binding_path = root / "employee_bindings.json"
        if binding_path.is_file():
            try:
                data = json.loads(binding_path.read_text(encoding="utf-8"))
                rows = data.get("bindings") if isinstance(data, dict) else None
                bindings = len(rows) if isinstance(rows, list) else 1
            except (OSError, ValueError):
                bindings = 1
        return {"instances": instances, "bindings": bindings}

    async def _rename_display_only(self, old_id: str, new_id: str, assets: dict[str, int]) -> ServiceResult:
        """Keep the id. The typed name becomes the display name and an alias."""
        aliases = load_aliases(self.context.ampuly_home)
        owner = aliases.get(new_id)
        if owner and owner != old_id:
            raise ServiceError("project_exists", f"Project '{new_id}' is already a display name", {"project_id": new_id})
        if self.context.project_dir(new_id).exists() or self.context.project_workplace(new_id).exists():
            raise ServiceError("project_exists", f"Project '{new_id}' already exists", {"project_id": new_id})
        memory_store = MarkdownMemoryStore(Path(self.context.root_engine.ampuly_home))
        if memory_store.memory_path(new_id).exists():
            raise ServiceError("project_exists", f"Project '{new_id}' already exists", {"project_id": new_id})
        BusinessStore(self.context.ampuly_home).set_project_display_name(old_id, new_id, actor="owner")
        register_alias(self.context.ampuly_home, new_id, old_id)
        active_id = self.context.active_engine_project_id()
        payload = {
            **self._project_list_payload(active_id),
            "action": "rename_project",
            "old_project_id": old_id,
            "project_id": old_id,
            "new_project_id": old_id,
            "display_name": new_id,
            "renamed": True,
            "identity_unchanged": True,
            "alias": new_id,
            "employee_assets": assets,
        }
        return ServiceResult(payload, [ServiceEvent("project_renamed", {
            "old_project_id": old_id, "project_id": old_id, "display_name": new_id, "identity_unchanged": True,
        })])

    async def rename(self, old_project_id: str, new_project_id: str) -> ServiceResult:
        old_id = str(old_project_id or "").strip()
        new_id = str(new_project_id or "").strip()
        if not old_id or not new_id:
            raise ServiceError("missing_project_id", "Missing project_id")
        if old_id == "default":
            raise ServiceError("default_project", "Cannot rename the default project")
        if new_id == "default":
            raise ServiceError("invalid_project_id", "Cannot rename a project to 'default'")
        if not self.context.is_safe_project_id(old_id) or not self.context.is_safe_project_id(new_id):
            raise ServiceError("invalid_project_id", "Invalid project_id (use alphanumeric, hyphens, underscores)")
        aliases = load_aliases(self.context.ampuly_home)
        if new_id in aliases and aliases[new_id] != old_id:
            raise ServiceError("project_exists", f"Project '{new_id}' is the display name of an existing project",
                               {"project_id": new_id})
        # 数据与文件 P3: 在业务库也跟着改对之前，有记录的项目不许改名——失败关闭。
        # 今天改名只重写了项目自己那个任务库，业务库从头到尾没被打开过，而工单是按记录里的项目字段认的。
        # 改完名，订单、生命周期、预算、流程采用全部留在旧名下，而且**没有任何报错**。真正的重写要动共用的
        # 业务库，那是第 4 期迁移批准的范围。在那之前，拒绝比悄悄弄坏好。
        if old_id != new_id:
            orphaned = self._rename_would_orphan(old_id)
            if orphaned:
                detail = "、".join(f"{name} {count} 条" for name, count in orphaned.items())
                raise ServiceError(
                    "rename_would_orphan_records",
                    f"这个项目在业务库里还有记录（{detail}），改名会把它们丢在旧名字底下。"
                    "改名要等业务库一起改对（第 4 期）；现在可以新建一个项目、把资料搬过去。",
                    {"project_id": old_id, "orphaned": orphaned})
            assets = self._adopted_employee_assets(old_id)
            if assets["instances"] or assets["bindings"] or old_id in aliases.values():
                return await self._rename_display_only(old_id, new_id, assets)
        if old_id == new_id:
            return ServiceResult({
                **self._project_list_payload(self.context.active_engine_project_id()),
                "action": "rename_project",
                "old_project_id": old_id,
                "project_id": new_id,
                "new_project_id": new_id,
                "renamed": False,
            })

        old_dir = self.context.project_dir(old_id)
        new_dir = self.context.project_dir(new_id)
        memory_store = MarkdownMemoryStore(Path(self.context.root_engine.ampuly_home))
        old_memory = memory_store.memory_path(old_id)
        new_memory = memory_store.memory_path(new_id)
        old_workplace = self.context.project_workplace(old_id)
        new_workplace = self.context.project_workplace(new_id)
        old_exists = old_dir.is_dir() or old_memory.exists() or old_workplace.exists()
        if not old_exists:
            raise ServiceError("project_not_found", f"Project '{old_id}' does not exist", {"project_id": old_id})
        if new_dir.exists() or new_memory.exists() or new_workplace.exists():
            raise ServiceError("project_exists", f"Project '{new_id}' already exists", {"project_id": new_id})
        chat_data_exists = getattr(self.context.chat_store, "project_data_exists", None)
        if callable(chat_data_exists) and await chat_data_exists(new_id):
            raise ServiceError("project_exists", f"Project '{new_id}' already has UI data", {"project_id": new_id})

        was_active = self.context.active_engine_project_id() == old_id
        if was_active:
            for task in list(self.context.background_tasks):
                task.cancel()
            self.context.background_tasks.clear()
            self.context.task_bg_map.clear()
            self.context.task_bg_context.clear()
        await self._close_project_engine_store(old_id)

        if old_dir.is_dir():
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            old_dir.rename(new_dir)
        else:
            new_dir.mkdir(parents=True, exist_ok=True)
        if old_memory.exists():
            new_memory.parent.mkdir(parents=True, exist_ok=True)
            old_memory.rename(new_memory)
        if self._owner_chose(old_id) is not None:
            # The owner named this folder; a new project id is no reason to rename their folder. The pin moves
            # instead, so the same files answer to the new name from the same place.
            self._repoint({old_id: new_id})
        else:
            if old_workplace.exists() and not self._same_folder(old_workplace, new_workplace):
                new_workplace.parent.mkdir(parents=True, exist_ok=True)
                from ampuly.plugins.office_ui.folder_move import promote

                # promote() falls back to a copy when the two ends are on different volumes; a bare rename would
                # leave a half-renamed project with no way back.
                promote(old_workplace, new_workplace)
            # Even an unpinned project can carry the 「旧文件夹里还有东西」 offer -- going back to the default root
            # drops the pin and records the old folder in the same write. Carry it, or the offer is filed under a
            # name nothing asks about any more.
            self._repoint({old_id: new_id})

        db_counts = self._rewrite_project_id_in_sqlite(new_dir / "tasks.db", old_id, new_id)
        chat_counts: dict[str, int] = {}
        rename_chat = getattr(self.context.chat_store, "rename_project_data", None)
        if callable(rename_chat):
            try:
                chat_counts = dict(await rename_chat(old_id, new_id) or {})
            except ValueError as exc:
                raise ServiceError("project_exists", str(exc), {"project_id": new_id}) from exc

        active_id = self.context.active_engine_project_id()
        events = [ServiceEvent("project_renamed", {"old_project_id": old_id, "project_id": new_id, "new_project_id": new_id})]
        payload: dict[str, Any] = {
            **self._project_list_payload(active_id),
            "action": "rename_project",
            "old_project_id": old_id,
            "project_id": new_id,
            "new_project_id": new_id,
            "renamed": True,
            "updated_task_tables": db_counts,
            "updated_ui_rows": chat_counts,
        }
        if was_active:
            engine = await self.context.activate_project(new_id)
            await self.context.chat_store.ensure_activity_channel(project_id=new_id)
            await self.context.chat_store.ensure_secretary_channel(project_id=new_id)
            payload["active_project_id"] = new_id
            payload["engine_project_id"] = getattr(engine, "project_id", new_id)
            events.append(ServiceEvent("project_switched", {"project_id": new_id}))
            snapshot = await build_snapshot(
                self.context.engine,
                self.context.agent_store,
                self.context.chat_store,
                self.context.event_adapter,
            )
            snapshot["exec_mode"] = self.context.mode_state.exec_mode
            snapshot["company_profile"] = self.context.mode_state.company_profile
            snapshot["task_preferred_agent"] = self.context.mode_state.task_preferred_agent
            events.append(ServiceEvent("snapshot", snapshot))
            collab = await build_collab_sync(
                self.context.engine,
                self.context.agent_store,
                self.context.chat_store,
                self.context.event_adapter,
                exec_mode=self.context.mode_state.exec_mode,
            )
            events.append(ServiceEvent("collab_sync_push", collab))
        return ServiceResult(payload, events)

    def _tree_counts(self, root: Path) -> dict[str, int]:
        """这棵树里有多少文件、多少字节。不跟随链接，读不了的照样计入件数。"""
        files = 0
        total = 0
        try:
            for here, _dirs, names in os.walk(root, followlinks=False):
                for name in names:
                    files += 1
                    try:
                        total += (Path(here) / name).stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return {"files": files, "bytes": total}

    def newest_backup(self, project_id: str) -> dict[str, Any] | None:
        """这个项目最新的一份备份，连同它还读不读得出来；一份都没有就是 None。"""
        from ampuly.plugins.office_ui import backup_list

        listed = backup_list.list_backups(self.context.ampuly_home, project_id=str(project_id or ""), limit=1)
        rows = list(listed.get("backups", []) or [])
        return rows[0] if rows else None

    async def plan_removal(self, project_id: str) -> ServiceResult:
        """删掉这个项目会带走什么——**只列不删**（数据与文件 P3）。

        两半都要说。会带走的：工作区（订单、交付、过程）、附件、业务记录、聊天、预算、客户记忆。**不会带走
        的**也要写出来，否则「都删干净了」是一句没人核对过的话：浏览器留痕、外部代理日志、已经做好的备份。

        还说清两件会拦住删除的事：有单在跑，和一份备份都没有。删除本身仍然要你打 `destroy:<项目名>`。
        """
        clean = str(project_id or "").strip()
        if not clean or not self.context.is_safe_project_id(clean):
            raise ServiceError("invalid_project_id", "Invalid project_id")
        store = BusinessStore(self.context.ampuly_home)
        memory_store = MarkdownMemoryStore(self.context.ampuly_home)
        customers = memory_store.customer_memory_dir(clean)
        chat = await self._export_chat(clean)
        goes = {
            "workplace": self._tree_counts(self.context.project_workplace(clean)),
            "project_dir": self._tree_counts(self.context.project_dir(clean)),
            "customer_memory": self._tree_counts(customers),
            "customer_service": self._tree_counts(self.context.ampuly_home / "customer_service" / clean),
            "business": dict(store.export_project_records(clean)["counts"]),
            "budgets": dict(store.export_project_budgets(clean)["counts"]),
            "chat": dict(chat.get("counts", {}) or {}),
        }
        home = self.context.ampuly_home
        stays = {
            "browser_artifacts": self._tree_counts(home / "browser" / "artifacts"),
            "external_agent_logs": self._tree_counts(home / "logs" / "external_agents"),
            "backups": {"files": len(list(backup_list_root(home).glob(f"{clean}-*.zip")))},
        }
        backup = self.newest_backup(clean)
        payload = {
            "action": "plan_removal",
            "project_id": clean,
            "goes": goes,
            "stays": stays,
            "blocked_by": self._removal_blocked(clean, backup),
            "backup": backup,
            # 内容令牌：真要删的那一批必须先重新列一次、比对令牌。这一期没有那一步，令牌先立在这里。
            "token": hashlib.sha256(_json_bytes(goes)).hexdigest()[:32],
        }
        return ServiceResult(payload)

    def _removal_blocked(self, project_id: str, backup: dict[str, Any] | None) -> list[dict[str, str]]:
        """现在不能删的理由，一条一句。空表示可以删。"""
        from ampuly.plugins.office_ui import folder_move

        blocked: list[dict[str, str]] = []
        try:
            busy = folder_move.busy_reason(BusinessStore(self.context.ampuly_home), self.context.ampuly_home, project_id)
        except Exception:  # noqa: BLE001 - 读不出来就当它在忙：不确定的时候不删
            busy = "project_busy"
        if busy:
            blocked.append({"code": busy, "why_zh": "这个项目还有单在跑；先等它结束或停掉它"})
        if backup is None:
            blocked.append({"code": "no_backup", "why_zh": "这个项目一份备份都没有。先点「备份」，再删"})
        elif backup.get("blocked"):
            blocked.append({"code": "backup_unreadable", "why_zh": "最新那份备份读不出来。先重新备份一次，再删"})
        return blocked

    async def delete(self, project_id: str, *, actor: str = "owner", confirmation: str = "",
                     require_backup: bool = True) -> ServiceResult:
        project_id = str(project_id or "").strip()
        if confirmation != f"destroy:{project_id}":
            raise ServiceError("confirmation_required", "Type destroy:<project_id> to permanently delete a project")
        if not project_id or project_id == "default":
            raise ServiceError("default_project", "Cannot delete the default project")
        if not self.context.is_safe_project_id(project_id):
            raise ServiceError("invalid_project_id", "Invalid project_id")

        was_active = self.context.active_engine_project_id() == project_id
        if was_active:
            for task in list(self.context.background_tasks):
                task.cancel()
            self.context.background_tasks.clear()
            self.context.task_bg_map.clear()
            self.context.task_bg_context.clear()

        store = BusinessStore(self.context.ampuly_home)
        current = self._lifecycle(project_id)
        if current.status == "destroyed":
            return ServiceResult({"project_id": project_id, "changed": False, "action": "delete_project"})
        # 数据与文件 P3: 删之前先看两件事——有没有单在跑，以及有没有一份读得出来的备份。
        # 过去两条销毁路径都不备份、也不看有没有在跑的单，而「删之前先备份」那句话在备份补全之前
        # 本来也保护不了客户记忆、聊天和预算。现在两样都补上了，这道闸才有意义。
        plan = (await self.plan_removal(project_id)).payload
        blocked = [item for item in plan["blocked_by"]
                   if require_backup or item["code"] not in {"no_backup", "backup_unreadable"}]
        if blocked:
            raise ServiceError("removal_blocked", "；".join(item["why_zh"] for item in blocked),
                               {"project_id": project_id, "blocked_by": blocked, "plan": plan})
        store.record_project_lifecycle(
            project_id, current.status, actor=self._actor(actor), reason="project_destroy_started", expected_status=current.status, event_action="project_destroy_started"
        )
        deleted_channels = 0
        delete_chat = getattr(self.context.chat_store, "delete_project_data", None)
        if callable(delete_chat):
            deleted_channels = int(await delete_chat(project_id) or 0)
            logger.info(f"Deleted {deleted_channels} channels for project '{project_id}'")
        purged_business = store.purge_project_operational_data(project_id)

        # Close every engine bound to this project AND evict its delegate from
        # the root engine's cache; a stale delegate would otherwise be reused
        # (with a closed store) if a project with the same id is re-created.
        await self._close_project_engine_store(project_id)

        projects_dir = self.context.project_dir(project_id)
        left_behind: list[str] = []
        if projects_dir.is_dir():
            shutil.rmtree(str(projects_dir), ignore_errors=True)
            if projects_dir.exists():
                # 删不掉不出声，等于「删掉了」这句话没人核对过。说出来，让人能去看一眼。
                left_behind.append(str(projects_dir))

        # A folder we made keeps today's answer (swallow); a folder the owner named never swallows.
        leftovers = self._remove_workplace(project_id, ours_may_swallow=True)
        if left_behind:
            leftovers = {**(leftovers or {}), "project_folder": left_behind}
        self._repoint({}, drop=(project_id,), actor=self._actor(actor))

        # 数据与文件 P3: 这个项目的记忆，两样都删。
        # 合成一套之前，两条路各漏一半：「删除」压根不删记忆文件（只指望一个引擎钩子），「销毁」删了文件却
        # 留下按客户分开的那些——于是后来用同一个名字新建的项目会继承上一个项目的客户记忆。现在删之前必须
        # 有一份备份，而备份从这一期起真的带走了客户记忆，所以这里删得干净才是安全的。
        memory_store = MarkdownMemoryStore(self.context.ampuly_home)
        service_dir = self.context.ampuly_home / "customer_service" / project_id
        if service_dir.is_dir():
            shutil.rmtree(str(service_dir), ignore_errors=True)
        for target in (memory_store.memory_path(project_id), memory_store.customer_memory_dir(project_id)):
            try:
                if target.is_dir():
                    shutil.rmtree(str(target), ignore_errors=True)
                elif target.exists():
                    target.unlink()
            except OSError:
                logger.opt(exception=True).debug(f"memory removal left something behind for {project_id}")

        memory = getattr(self.context.engine, "memory", None)
        if memory:
            delete_fn = getattr(memory, "delete_project", None)
            if callable(delete_fn):
                try:
                    maybe = delete_fn(project_id)
                    if asyncio.iscoroutine(maybe):
                        await maybe
                except Exception:
                    logger.opt(exception=True).debug(f"memory.delete_project failed for {project_id}")

        store.record_project_lifecycle(
            project_id, "destroyed", actor=self._actor(actor), reason="project_deleted", expected_status=current.status, event_action="project_destroyed", metadata={"deleted_channels": deleted_channels, "purged_business": purged_business}
        )
        events = [ServiceEvent("project_deleted", {"project_id": project_id})]
        payload: dict[str, Any] = {"project_id": project_id, "deleted_channels": deleted_channels, "purged_business": purged_business, "changed": True, "action": "delete_project"}
        if leftovers:
            payload["leftovers"] = leftovers
        if was_active:
            self.context.project_dir("default").mkdir(parents=True, exist_ok=True)
            await self.context.activate_project("default")
            await self.context.chat_store.ensure_activity_channel(project_id="default")
            await self.context.chat_store.ensure_secretary_channel(project_id="default")
            payload["active_project_id"] = "default"
            events.append(ServiceEvent("project_switched", {"project_id": "default"}))
            snapshot = await build_snapshot(
                self.context.engine,
                self.context.agent_store,
                self.context.chat_store,
                self.context.event_adapter,
            )
            snapshot["exec_mode"] = self.context.mode_state.exec_mode
            snapshot["company_profile"] = self.context.mode_state.company_profile
            snapshot["task_preferred_agent"] = self.context.mode_state.task_preferred_agent
            events.append(ServiceEvent("snapshot", snapshot))
            collab = await build_collab_sync(
                self.context.engine,
                self.context.agent_store,
                self.context.chat_store,
                self.context.event_adapter,
                exec_mode=self.context.mode_state.exec_mode,
            )
            events.append(ServiceEvent("collab_sync_push", collab))
        return ServiceResult(payload, events)

    async def _leave_before_lifecycle_change(self, project_id: str) -> str:
        """Archiving or destroying the project the console is on: move to
        ``default`` first and report it, instead of refusing.

        The selector offers 归档/删除 only for the active project, so a refusal
        ("switch to another project first") made those controls impossible to
        use (2026-09-06 drill).  Returns the project switched to, or "".
        """
        if self.context.active_engine_project_id() != project_id:
            return ""
        await self.switch("default", switch_seq="", include_snapshot=False)
        return "default"

    async def archive(self, project_id: str, *, actor: str = "owner", reason: str = "") -> ServiceResult:
        """Freeze a project without deleting data; archived projects cannot run."""
        clean = str(project_id or "").strip()
        if not clean or clean == "default":
            raise ServiceError("default_project", "Cannot archive the default project")
        if not self.context.is_safe_project_id(clean):
            raise ServiceError("invalid_project_id", "Invalid project_id")
        if not self.context.project_dir(clean).is_dir():
            raise ServiceError("project_not_found", f"Project '{clean}' does not exist", {"project_id": clean})
        current = self._lifecycle(clean)
        if current.status == "destroyed":
            raise ServiceError("project_destroyed", f"Project '{clean}' is destroyed", {"project_id": clean})
        if current.status == "archived":
            return ServiceResult({**self._project_list_payload(self.context.active_engine_project_id()), "action": "archive_project", "project_id": clean, "changed": False})
        switched_to = await self._leave_before_lifecycle_change(clean)
        record = BusinessStore(self.context.ampuly_home).record_project_lifecycle(
            clean,
            "archived",
            actor=self._actor(actor),
            reason=reason or "project_archived",
            expected_status="active",
        )
        return ServiceResult(
            {**self._project_list_payload(self.context.active_engine_project_id()), "action": "archive_project", "project_id": clean, "changed": True, "switched_to": switched_to, "lifecycle": record.to_payload()},
            [ServiceEvent("project_lifecycle_changed", record.to_payload())],
        )

    async def restore(self, project_id: str, *, actor: str = "owner", reason: str = "") -> ServiceResult:
        clean = str(project_id or "").strip()
        if not clean or clean == "default":
            raise ServiceError("default_project", "Cannot restore the default project")
        if not self.context.is_safe_project_id(clean):
            raise ServiceError("invalid_project_id", "Invalid project_id")
        if not self.context.project_dir(clean).is_dir():
            raise ServiceError("project_not_found", f"Project '{clean}' does not exist", {"project_id": clean})
        current = self._lifecycle(clean)
        if current.status == "destroyed":
            raise ServiceError("project_destroyed", f"Project '{clean}' is destroyed", {"project_id": clean})
        if current.status == "active":
            return ServiceResult({**self._project_list_payload(self.context.active_engine_project_id()), "action": "restore_project", "project_id": clean, "changed": False})
        record = BusinessStore(self.context.ampuly_home).record_project_lifecycle(
            clean,
            "active",
            actor=self._actor(actor),
            reason=reason or "project_restored",
            expected_status="archived",
        )
        return ServiceResult(
            {**self._project_list_payload(self.context.active_engine_project_id()), "action": "restore_project", "project_id": clean, "changed": True, "lifecycle": record.to_payload()},
            [ServiceEvent("project_lifecycle_changed", record.to_payload())],
        )

    @staticmethod
    def _archive_sources(root: Path, prefix: str) -> list[tuple[Path, str]]:
        if not root.exists():
            return []
        sources: list[tuple[Path, str]] = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ServiceError(
                    "unsafe_export_path",
                    f"Project export contains a redirected path: {path.relative_to(root).as_posix()}",
                )
            if path.is_file():
                if path.name in LIVE_RUNTIME_FILES:
                    continue  # 由 RUNTIME_DB_MEMBER 以一致快照带走；按文件拷会拷到写了一半的那一份
                sources.append((path, f"{prefix}/{path.relative_to(root).as_posix()}"))
        return sources

    @classmethod
    def _customer_service_archive_sources(
        cls, root: Path, prefix: str, scratch: Path,
    ) -> list[tuple[Path, str]]:
        """Freeze each live store; process locks and SQLite sidecars never travel."""
        sources: list[tuple[Path, str]] = []
        for source, member in cls._archive_sources(root, prefix):
            if source.name.endswith(("-wal", "-shm", "-journal", ".owner")):
                continue
            if source.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
                snapshot = scratch / source.relative_to(root)
                if not _sqlite_snapshot(source, snapshot):
                    raise FileNotFoundError(f"Customer-service database disappeared: {source.name}")
                sources.append((snapshot, member))
            else:
                sources.append((source, member))
        return sources

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _zip_info(name: str) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        return info

    @classmethod
    def _verify_archive(cls, path: Path, manifest: dict[str, Any]) -> None:
        expected = list(manifest.get("files", []) or [])
        expected_names = ["manifest.json", *[str(item.get("path", "")) for item in expected]]
        try:
            with zipfile.ZipFile(path) as archive:
                if archive.namelist() != expected_names:
                    raise ServiceError("invalid_backup", "Backup member list does not match its manifest")
                for item in expected:
                    member = str(item.get("path", ""))
                    info = archive.getinfo(member)
                    if info.file_size != int(item.get("size", -1)):
                        raise ServiceError("invalid_backup", f"Backup size mismatch: {member}")
                    digest = hashlib.sha256()
                    with archive.open(info) as stream:
                        while chunk := stream.read(1024 * 1024):
                            digest.update(chunk)
                    if digest.hexdigest() != str(item.get("sha256", "")):
                        raise ServiceError("invalid_backup", f"Backup content digest mismatch: {member}")
        except zipfile.BadZipFile as exc:
            raise ServiceError("invalid_backup", "Backup is not a valid ZIP archive") from exc

    async def _export_chat(self, project_id: str) -> dict[str, Any]:
        """This project's console conversations. Empty (and said so) when the console has no chat store."""
        export = getattr(getattr(self.context, "chat_store", None), "export_project_data", None)
        if not callable(export):
            return {"schema_version": 1, "project_id": project_id, "tables": {},
                    "counts": {}, "unavailable": "no_chat_store"}
        try:
            return dict(await export(project_id))
        except Exception as exc:  # noqa: BLE001 - a chat store that will not read is said so, not guessed at
            logger.opt(exception=True).warning("backup: this project's chat could not be read")
            return {"schema_version": 1, "project_id": project_id, "tables": {},
                    "counts": {}, "unavailable": type(exc).__name__}

    def _export_allowlist(self, project_id: str) -> dict[str, Any]:
        """The approvals granted to THIS project, and nothing else.

        Only this project's slice travels. The file it comes from sits beside the model configuration, and a
        whole-file copy would carry keys into an archive people pass around.
        """
        path = self.context.ampuly_home / "config" / "approval_allowlist.yaml"
        try:
            import yaml

            raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except Exception as exc:  # noqa: BLE001 - an unreadable allow-list is reported, never guessed at
            return {"schema_version": 1, "project_id": project_id, "grants": {},
                    "counts": {}, "unavailable": type(exc).__name__}
        projects = dict(dict(raw or {}).get("projects", {}) or {})
        mine = projects.get(project_id)
        grants = dict(mine) if isinstance(mine, dict) else {}
        return {"schema_version": 1, "project_id": project_id, "grants": grants,
                "counts": {"grants": len(grants)}}

    def _coverage(self, project_id: str, counts: dict[str, dict[str, int]], *, carried: list[str]) -> dict[str, Any]:
        """What this archive holds, class by class, and -- for what it does not -- why not.

        Read off :mod:`ampuly.core.data_map`, so it cannot drift from the declaration the way the old hand-written
        「备份里没有什么」 list did (it claimed orders and gate cards were missing long after they were in).
        Three answers per class: ``included`` (with how much), ``empty`` (this project has none) or
        ``excluded`` (with the reason the declaration gives).
        """
        from ampuly.core.data_map import DATA_MAP

        member_for = {"business_db": BUSINESS_RECORDS_MEMBER, "ui_state_db": CHAT_MEMBER,
                      "approval_allowlist": ALLOWLIST_MEMBER}
        prefix_for = {"project_workplace": f"workplace/{project_id}/", "project_dir": f"project/{project_id}/",
                      "attachments": f"project/{project_id}/attachments/",
                      "customer_memory": f"memory/{project_id}/customers/",
                      "project_memory": f"memory/{project_id}.md",
                      "customer_service": f"customer_service/{project_id}/"}
        # 运行库是一整个文件，不是一堆行，所以按「带没带这个成员」算，不按行数算。
        prefix_for["project_tasks_db"] = RUNTIME_DB_MEMBER
        out: dict[str, Any] = {}
        for entry in DATA_MAP:
            if entry.container:
                continue
            member = member_for.get(entry.key)
            if member is not None:
                rows = counts.get(member, {})
                total = sum(int(value) for value in rows.values())
                out[entry.key] = {"state": "included" if total else "empty", "member": member, "counts": rows}
                continue
            prefix = prefix_for.get(entry.key)
            if prefix is not None:
                files = len([name for name in carried if name == prefix or name.startswith(prefix)])
                out[entry.key] = {"state": "included" if files else "empty", "files": files}
                if entry.key == "customer_service":
                    out[entry.key]["label_zh"] = entry.label_zh
                    out[entry.key]["send_authority"] = "requires_recheck"
                    out[entry.key]["restore_note"] = (
                        "历史记录会放回去。发送权、账号租约和未完成的外发不会自动恢复，要重新核权。"
                    )
                continue
            out[entry.key] = ({"state": "excluded", "reason": entry.reason} if not entry.in_backup
                              else {"state": "empty"})
        # These two have no data_map class of their own: they are the project's own record, carried since P3.
        for key, member, label in (("project_history", HISTORY_MEMBER, "项目自己的生命周期记录"),
                                   ("budgets", BUDGETS_MEMBER, "预算与预留")):
            rows = counts.get(member, {})
            out[key] = {"state": "included" if sum(int(v) for v in rows.values()) else "empty",
                        "member": member, "counts": rows, "label_zh": label}
        out["chat"] = {**out.get("ui_state_db", {}), "label_zh": "聊天记录"}
        # 「在备份里」和「还原后就回来了」不是同一个承诺。哪几样会被放回去，读的是还原那一侧的同一份名单
        # （RESTORED_MEMBERS / NOT_RESTORED），不是在这里另写一遍——两边各写一份的话，改了一边忘了另一
        # 边，备份就会替还原撒谎。2026-09-24 就差点这样：聊天已经能还原了，而这里还写着「不会放回去」。
        explained = dict(NOT_RESTORED)
        for state in out.values():
            member = str(state.get("member", "") or "")
            if not member:
                continue
            state["restored"] = member in RESTORED_MEMBERS
            if member in explained:
                state["restore_note"] = explained[member]
        return out

    async def export(self, project_id: str, *, actor: str = "owner", output_dir: str | Path | None = None) -> ServiceResult:
        clean = str(project_id or "").strip()
        if not clean or not self.context.is_safe_project_id(clean):
            raise ServiceError("invalid_project_id", "Invalid project_id")
        if clean != "default" and not self.context.project_dir(clean).is_dir():
            raise ServiceError("project_not_found", f"Project '{clean}' does not exist", {"project_id": clean})
        pinned = self._owner_chose(clean)
        if pinned is not None and not pinned.is_dir():
            # _archive_sources opens with `if not root.exists(): return []`, so an unplugged disk would otherwise
            # produce a 「verified」 archive with the project's files silently missing from it.
            raise ServiceError("workplace_missing", f"Project '{clean}' is pointed at a folder that is not there",
                               {"project_id": clean})
        destination = Path(output_dir) if output_dir else self.context.ampuly_home / "exports"
        destination = destination.resolve()
        allowed_root = (self.context.ampuly_home / "exports").resolve()
        try:
            destination.relative_to(allowed_root)
        except ValueError as exc:
            raise ServiceError("invalid_output_dir", "Export directory must be inside .ampuly/exports") from exc
        destination.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        output_path = destination / f"{clean}-{stamp}.zip"
        project_root = self.context.project_dir(clean)
        memory_store = MarkdownMemoryStore(self.context.ampuly_home)
        memory = memory_store.memory_path(clean)
        # 数据与文件 P3: the per-customer memories. `memory_path(clean)` takes only the no-customer bucket, so
        # every customer's own memory used to be destroyed by a delete and carried by no backup.
        customers = memory_store.customer_memory_dir(clean)
        workplace = self.context.project_workplace(clean)
        lifecycle_payload = self._lifecycle(clean).to_payload()
        if lifecycle_payload.get("latest_backup_path"):
            lifecycle_payload["latest_backup_path"] = Path(str(lifecycle_payload["latest_backup_path"])).name
        sources = [
            *self._archive_sources(project_root, f"project/{clean}"),
            *([(memory, f"memory/{memory.name}")] if memory.is_file() else []),
            *self._archive_sources(customers, f"memory/{clean}/customers"),
            *self._archive_sources(workplace, f"workplace/{clean}"),
        ]
        # Snapshot CRM rather than copying a live database or its sidecars.
        crm_member = f"project/{clean}/plugins/crm/sales.sqlite"
        sources = [(source, member) for source, member in sources
                   if member not in {crm_member, *(crm_member + suffix for suffix in ("-journal", "-wal", "-shm"))}]
        # 这个项目的运行库。快照落在一个只活到打包结束的临时目录里，好让指纹、复制和校验读到的是同一份
        # 不再变化的文件。
        #
        # 默认项目的那一份在哪，问 `default_runtime_db_path`，不要写死 `global.db`：搬迁之后 global.db 还在
        # （那是退路，不再更新），写死的话备份会认认真真装走一份旧文件，而真正在用的那份一次都没进过备份。
        scratch = tempfile.TemporaryDirectory(prefix=".backup-scratch-")
        sources.extend(self._customer_service_archive_sources(
            self.context.ampuly_home / "customer_service" / clean,
            f"customer_service/{clean}", Path(scratch.name) / "customer-service",
        ))
        crm_snapshot = Path(scratch.name) / "crm-sales.sqlite"
        if _sqlite_snapshot(project_root / "plugins" / "crm" / "sales.sqlite", crm_snapshot):
            sources.append((crm_snapshot, crm_member))
        # L4: seal CRM sqlite + hmac-key so an archive never carries the digest key in plaintext.
        # Wrapping key lives under AMPULY_HOME/plugins/crm/backup-keys/ — outside the zip and outside
        # backups/projects. Digests in the manifest cover the sealed bytes that actually travel.
        from ampuly.plugins.crm_agent.backup_crypto import (
            is_crm_sensitive_member,
            load_or_create_project_key,
            seal as seal_crm_backup_member,
        )
        crm_wrap_key = None
        sealed_sources: list[tuple[Path, str]] = []
        for source, member in sources:
            if is_crm_sensitive_member(member):
                if crm_wrap_key is None:
                    crm_wrap_key = load_or_create_project_key(self.context.ampuly_home, clean)
                sealed_path = Path(scratch.name) / f"sealed-{Path(member).name}"
                sealed_path.write_bytes(
                    seal_crm_backup_member(
                        source.read_bytes(),
                        key=crm_wrap_key,
                        aad=member.encode("utf-8"),
                    )
                )
                sealed_sources.append((sealed_path, member))
            else:
                sealed_sources.append((source, member))
        sources = sealed_sources
        runtime_db = Path(scratch.name) / "tasks.db"
        live_runtime = (default_runtime_db_path(self.context.ampuly_home) if clean == "default"
                        else project_root / "tasks.db")
        if _sqlite_snapshot(live_runtime, runtime_db):
            sources.append((runtime_db, RUNTIME_DB_MEMBER))
        files = [
            {"path": member, "size": source.stat().st_size, "sha256": self._file_digest(source)}
            for source, member in sources
        ]
        # The project's business records -- its orders, their signed event chains, gate cards and gate events --
        # live in the console's shared business store, not under the project's folders, so an export used to leave
        # them behind while deleting the project purged them.  They travel as one JSON member, digested like a file.
        business = BusinessStore(self.context.ampuly_home)
        records = business.export_project_records(clean)
        # 数据与文件 P3: the four the owner named that no backup carried -- this project's money, its own history,
        # its console conversations, and the approvals it was granted. Each travels as one JSON member, digested
        # like a file, so the archive's verification covers them too.
        budgets = business.export_project_budgets(clean)
        history = business.export_project_history(clean)
        chat = await self._export_chat(clean)
        grants = self._export_allowlist(clean)
        bodies: list[tuple[str, bytes, dict[str, int]]] = [
            (BUSINESS_RECORDS_MEMBER, _json_bytes(records), dict(records["counts"])),
            (BUDGETS_MEMBER, _json_bytes(budgets), dict(budgets["counts"])),
            (HISTORY_MEMBER, _json_bytes(history), dict(history["counts"])),
            (CHAT_MEMBER, _json_bytes(chat), dict(chat["counts"])),
            (ALLOWLIST_MEMBER, _json_bytes(grants), dict(grants["counts"])),
        ]
        for member, body, _counts in bodies:
            files.append({"path": member, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        manifest = {
            # 3: carries customer memory, chat, budgets, project history and approvals, and says in `coverage`
            # what it holds and -- for everything it does not -- why not.
            "schema_version": 3,
            "project_id": clean,
            "lifecycle": lifecycle_payload,
            "created_at": stamp,
            "files": files,
            "coverage": self._coverage(clean, {member: counts for member, _b, counts in bodies},
                                       carried=[member for _s, member in sources]),
            "total_bytes": sum(int(item["size"]) for item in files),
        }
        manifest_body = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        manifest["content_digest"] = hashlib.sha256(manifest_body).hexdigest()
        try:
            with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    self._zip_info("manifest.json"),
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                )
                for source, member in sources:
                    info = self._zip_info(member)
                    with source.open("rb") as input_stream, archive.open(info, "w") as output_stream:
                        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
                for member, body, _counts in bodies:
                    archive.writestr(self._zip_info(member), body)
            self._verify_archive(output_path, manifest)
        except BaseException:
            output_path.unlink(missing_ok=True)
            raise
        finally:
            scratch.cleanup()
        archive_digest = self._file_digest(output_path)
        payload = {
            "action": "export_project",
            "project_id": clean,
            "path": str(output_path),
            "files": len(files),
            "sha256": archive_digest,
            "verified": True,
            "business_records": dict(records["counts"]),
        }
        BusinessStore(self.context.ampuly_home).record_project_lifecycle(clean, self._lifecycle(clean).status, actor=self._actor(actor), reason="project_exported", latest_backup_path=str(output_path), metadata={"files": len(files), "sha256": archive_digest}, event_action="project_exported")
        return ServiceResult(payload, [ServiceEvent("project_exported", payload)])

    async def backup(self, project_id: str, *, actor: str = "owner") -> ServiceResult:
        destination = self.context.ampuly_home / "backups" / "projects"
        # Build the archive through the same export path, then move the closed
        # file into the immutable backup area.  This keeps all archive writes
        # under .ampuly and avoids exposing arbitrary filesystem paths to callers.
        result = await self.export(project_id, actor=actor)
        path = Path(str(result.payload.get("path", "")))
        destination.mkdir(parents=True, exist_ok=True)
        # export() intentionally restricts output to .ampuly/exports; move the
        # archive into the dedicated backup directory only after it is closed.
        target = destination.resolve() / path.name
        shutil.move(str(path), str(target))
        lifecycle = self._lifecycle(project_id)
        BusinessStore(self.context.ampuly_home).record_project_lifecycle(
            str(project_id), lifecycle.status, actor=self._actor(actor), reason="project_backed_up", latest_backup_path=str(target), metadata={"source": "backup"}, event_action="project_backed_up"
        )
        from ampuly.plugins.office_ui.backup_retention import prune_project_backups
        retention = prune_project_backups(destination)
        result.payload["action"] = "backup_project"
        result.payload["path"] = str(target)
        result.payload["retention"] = {
            "keep_days": retention.get("keep_days"),
            "keep_min": retention.get("keep_min"),
            "deleted": retention.get("deleted"),
            "kept": retention.get("kept"),
        }
        result.events = [ServiceEvent("project_backed_up", dict(result.payload))]
        return result

    async def destroy(self, project_id: str, *, actor: str = "owner", confirmation: str = "",
                      require_backup: bool = True) -> ServiceResult:
        """「销毁」就是「删除」（数据与文件 P3）。

        它们本来是两条几乎重合、却在八个地方不一致的路：一条取消后台任务一条不取消；一条吞掉删不掉的错误
        一条不吞；一条删客户记忆一条不删；先切走还是后切走也不同。于是**按哪个按钮，决定了什么数据能活
        下来**——这不是设计，是两次分别写出来的结果。

        现在只有一条路。这条路会先列出将要带走什么、不带走什么，要求有单不在跑、有一份读得出来的备份，
        再要你打 `destroy:<项目名>`。
        """
        result = await self.delete(project_id, actor=actor, confirmation=confirmation,
                                   require_backup=require_backup)
        payload = {**result.payload, "action": "destroy_project"}
        return ServiceResult(payload, list(result.events))

    async def restore_backup(
        self,
        archive_path: str | Path,
        *,
        project_id: str | None = None,
        actor: str = "owner",
    ) -> ServiceResult:
        """Restore a project from a locally-created backup without overwriting anything.

        Verifying and unpacking an archive reads every byte of it; on the event loop that froze every socket of the
        console for as long as it took, and a client whose heartbeat went unanswered dropped its connection and never
        saw the receipt. The file work runs in a worker thread.

        Putting the files in place and writing the project's record are one unit inside that thread. A thread cannot
        be cancelled; the coroutine waiting for it can (the console shutting down mid-restore). Done in two steps,
        that left the files in place under a project that still read "destroyed": it could not be opened, a second
        restore was refused because the files existed, and nothing in the console could clear it.
        """
        clean, source, record = await asyncio.to_thread(self._restore_backup_sync, archive_path, project_id, self._actor(actor))
        names = set(await asyncio.to_thread(self._member_names, source))
        chat_counts, chat_missing = await self._restore_chat(clean, source, present=CHAT_MEMBER in names)
        payload = {
            "action": "restore_backup", "project_id": clean, "source": str(source),
            "lifecycle": record.to_payload(),
            "restored": {"chat": chat_counts},
            # 备份里有、但没有放回去的东西，一条一个理由。恢复完不说这一句，等于又造了一次「看起来是全的」。
            "not_restored": [*chat_missing,
                             *({"member": member, "why_zh": why} for member, why in NOT_RESTORED if member in names)],
        }
        return ServiceResult(payload, [ServiceEvent("project_restored_from_backup", payload)])

    @staticmethod
    def _member_names(source: Path) -> tuple[str, ...]:
        """这份归档里有哪些成员。用来只谈它真的带着的东西——旧备份没有的成员不该出现在回执里。"""
        with zipfile.ZipFile(source) as archive:
            return tuple(str(name).replace("\\", "/") for name in archive.namelist())

    @staticmethod
    def _read_member(source: Path, member: str) -> dict[str, Any] | None:
        """归档里的一个 JSON 成员；这份备份没有它就返回 ``None``。"""
        with zipfile.ZipFile(source) as archive:
            if member not in archive.namelist():
                return None
            return json.loads(archive.read(member).decode("utf-8"))

    async def _restore_chat(self, project_id: str, source: Path, *, present: bool) -> tuple[dict[str, int], list[dict[str, str]]]:
        """把备份里的聊天放回去；放不回去就**明说**，而不是让项目悄悄少一块。

        在事件循环上做，不在工作线程里：聊天库是控制台自己的异步数据库，工作线程没有资格开它。
        """
        if not present:
            return {}, []
        data = await asyncio.to_thread(self._read_member, source, CHAT_MEMBER)
        if not isinstance(data, dict) or data.get("unavailable") or not any(dict(data.get("counts") or {}).values()):
            # 这份备份里本来就没有聊天：要么这个项目一句话没说过，要么备份那天聊天库就读不出来（当时已经
            # 记下了）。没有东西要放回去，也就没有什么「没放回去」——报一句不存在的损失，和瞒下一个真损失
            # 是同一类毛病，只是方向反了。
            return {}, []
        importer = getattr(getattr(self.context, "chat_store", None), "import_project_data", None)
        if not callable(importer):
            return {}, [{"member": CHAT_MEMBER, "why_zh": "这台控制台没有聊天库，备份里的聊天没有放回去"}]
        try:
            return dict(await importer(project_id, data) or {}), []
        except Exception as exc:  # noqa: BLE001 - 聊天放不回去不撤销整次恢复，见下
            # 项目没有聊天也还是完整的。为了一段读不回来的对话，把已经放回原位的订单、交付和记忆再端走，
            # 是拿一个真损失换一个小损失。所以这里只说、不撤——说清楚本身就是这一期要修的东西。
            logger.opt(exception=True).warning("restore: this project's chat could not be put back")
            return {}, [{"member": CHAT_MEMBER,
                         "why_zh": f"聊天没有放回去：{type(exc).__name__}。项目本身已经恢复，聊天可以从这份备份里单独再试"}]

    def _restore_backup_sync(self, archive_path: str | Path, project_id: str | None, actor: str) -> tuple[str, Path, Any]:
        """Files in place, then the record -- or neither. Synchronous: call it from a worker thread."""
        clean, source = self._restore_backup_files(archive_path, project_id)
        # The business records come back with the files, or the files go back out: a project restored without its
        # orders would look whole and be missing its history.  An older backup has no such member and restores as
        # it always did.  Messages that were waiting to be sent are never brought back (see the store).
        records: dict[str, Any] | None = None
        restored_records: dict[str, int] = {}
        try:
            with zipfile.ZipFile(source) as archive:
                if BUSINESS_RECORDS_MEMBER in archive.namelist():
                    records = json.loads(archive.read(BUSINESS_RECORDS_MEMBER).decode("utf-8"))
            if records is not None:
                restored_records = BusinessStore(self.context.ampuly_home).import_project_records(clean, records)
        except Exception as exc:
            self._remove_restored_files(clean)
            raise ServiceError("restore_failed", f"The files were restored but the project's business records could not be, so they were removed again: {exc}") from exc
        previous_customer = ""
        try:
            previous_customer = str(BusinessStore(self.context.ampuly_home).project_lifecycle(clean).customer_id or "")
        except Exception:
            previous_customer = ""
        metadata: dict[str, Any] = {"backup": source.name, "business_records": dict(restored_records)}
        if previous_customer:
            # The binding survives a destroy on purpose (an unbound project would be open to every customer's
            # account); the trail says that it came back with the files.
            metadata["customer_id"] = previous_customer
        crm_replay = self._reapply_crm_after_restore(clean, actor)
        if any(crm_replay.values()):
            metadata["crm_l4"] = crm_replay
        try:
            record = BusinessStore(self.context.ampuly_home).record_project_lifecycle(
                clean,
                "active",
                actor=actor,
                reason="project_restored_from_backup",
                metadata=metadata,
                event_action="project_restored_from_backup",
                allow_destroyed_restore=True,
            )
        except Exception as exc:
            # Files in place under a project that still reads "destroyed" could never be opened, and a second
            # restore would be refused because they exist. Take them back out: they were absent a moment ago.
            self._remove_restored_files(clean)
            if records is not None:
                BusinessStore(self.context.ampuly_home).discard_imported_project_records(records)
            raise ServiceError("restore_failed", f"The files were restored but the project record could not be written, so they were removed again: {exc}") from exc
        return clean, source, record


    def _reapply_crm_after_restore(self, project_id: str, actor: str) -> dict[str, Any]:
        """Decrypt any leftover sealed CRM files and force tombstone replay (CRM L4).

        Tombstones already re-apply when a CRM context opens; doing it here means a
        restored tree is erased before the next console request, and the restore
        receipt can report how many records were scrubbed.
        """
        from ampuly.plugins.crm_agent.backup_crypto import open_crm_tree_in_place
        from ampuly.plugins.crm_agent.contracts import CRMError, Scope
        from ampuly.plugins.crm_agent.store import CRMStore

        summary: dict[str, Any] = {"crm_opened": 0, "tombstones_reapplied": 0}
        crm_dir = self.context.project_dir(project_id) / "plugins" / "crm"
        if not crm_dir.is_dir():
            return summary
        try:
            summary["crm_opened"] = int(
                open_crm_tree_in_place(crm_dir, ampuly_home=self.context.ampuly_home, project_id=project_id) or 0
            )
        except CRMError as exc:
            raise ServiceError(exc.code, f"CRM backup could not be opened: {exc.code}") from exc
        db = crm_dir / "sales.sqlite"
        if not db.is_file():
            return summary
        try:
            customer_id = str(
                BusinessStore(self.context.ampuly_home).project_lifecycle(project_id).customer_id or ""
            )
        except Exception:
            customer_id = ""
        if not customer_id:
            return summary
        tombstones = self.context.ampuly_home / "plugins" / "crm" / "tombstones" / f"{project_id}.json"
        try:
            store = CRMStore(db, admission="synthetic_only", tombstones=tombstones)
            scope = Scope(project_id, customer_id, actor, True, (actor,))
            summary["tombstones_reapplied"] = int(store.apply_tombstones(scope) or 0)
        except CRMError as exc:
            raise ServiceError(
                exc.code, f"CRM erasure tombstones could not be re-applied: {exc.code}"
            ) from exc
        return summary

    def _restore_trees(self, project_id: str, staging: Path) -> tuple[tuple[str, str, Path, Path], ...]:
        """备份里的几棵树：叫什么、在归档里长什么前缀、解压后落在哪、最终要放到哪。

        **只有这一处列表。** 曾经是两处：一处说归档里允许出现 ``memory/<项目>/customers/``，另一处负责把
        解压出来的东西挪到位——而后者少了这一棵。于是客户记忆被打进备份、被解压到暂存区，再随暂存区一起
        删掉；备份里有、恢复回来没有，中间没有任何一句话提到它。

        合成一处之后，再漏一棵就得同时漏在两处；而那样的话，归档里那一棵会被判成「不该出现的路径」，恢复
        当场就拒绝。写错比写漏响得早。
        """
        memory = MarkdownMemoryStore(self.context.ampuly_home)
        return (
            ("project folder", f"project/{project_id}/",
             staging / "project" / project_id, self.context.project_dir(project_id)),
            ("workplace", f"workplace/{project_id}/",
             staging / "workplace" / project_id, self.context.project_workplace(project_id)),
            ("memory file", f"memory/{project_id}.md",
             staging / "memory" / f"{project_id}.md", memory.memory_path(project_id)),
            ("customer memory", f"memory/{project_id}/customers/",
             staging / "memory" / project_id / "customers", memory.customer_memory_dir(project_id)),
            ("customer service", f"customer_service/{project_id}/",
             staging / "customer_service" / project_id,
             self.context.ampuly_home / "customer_service" / project_id),
        )

    def restore_target_occupied(self, project_id: str) -> list[str]:
        """Which of the places a restore writes to already hold something. The preflight asks the same question."""
        clean = str(project_id or "").strip()
        if not clean or not self.context.is_safe_project_id(clean):
            return []
        staging = self.context.ampuly_home / ".restore-staging"
        return [label for label, _prefix, _staged, target in self._restore_trees(clean, staging) if target.exists()]

    def _remove_restored_files(self, project_id: str) -> None:
        staging = self.context.ampuly_home / ".restore-staging"
        for _label, _prefix, _staged, target in self._restore_trees(project_id, staging):
            if target.is_dir():
                shutil.rmtree(str(target), ignore_errors=True)
            elif target.exists():
                target.unlink()

    @staticmethod
    def _promote(staged: Path, target: Path) -> None:
        """Move a restored tree to its final name: never over something, through a scanner's grip, across volumes.

        ``Path.rename`` is kept because on Windows it refuses an existing target, which is the guarantee a restore
        makes. A scanner or the indexer holds freshly unpacked files for a few milliseconds and the rename is
        refused with WinError 5; that is retried. The workplace root may sit on another volume than the staging
        folder (``AMPULY_WORKPLACE_ROOT``); a rename cannot cross volumes, a copy-then-delete can.
        """
        for attempt in range(6):
            if target.exists():
                raise FileExistsError(f"{target.name} appeared while the backup was being restored")
            try:
                staged.rename(target)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
            except OSError as exc:
                if exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17:
                    try:
                        shutil.move(str(staged), str(target))
                    except Exception:
                        # a half-made copy is this restore's own; the target did not exist a moment ago
                        if target.is_dir():
                            shutil.rmtree(str(target), ignore_errors=True)
                        elif target.exists():
                            target.unlink()
                        raise
                    return
                raise

    def _restore_backup_files(self, archive_path: str | Path, project_id: str | None) -> tuple[str, Path]:
        """Verify the archive and put its three trees in place. Synchronous: call it from a worker thread."""
        source = Path(archive_path).resolve()
        backup_root = (self.context.ampuly_home / "backups" / "projects").resolve()
        try:
            source.relative_to(backup_root)
        except ValueError as exc:
            raise ServiceError("invalid_backup_path", "Backup must be inside .ampuly/backups/projects") from exc
        if not source.is_file():
            raise ServiceError("backup_not_found", f"Backup '{source.name}' does not exist")
        try:
            with zipfile.ZipFile(source) as archive:
                names = archive.namelist()
                if "manifest.json" not in names:
                    raise ServiceError("invalid_backup", "Backup manifest is missing")
                try:
                    manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ServiceError("invalid_backup", "Backup manifest is invalid") from exc
                manifest_id = str(manifest.get("project_id", "") or "").strip()
                schema_version = int(manifest.get("schema_version", 0) or 0)
                if schema_version >= 2:
                    body = {key: value for key, value in manifest.items() if key != "content_digest"}
                    actual = hashlib.sha256(
                        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest()
                    if actual != str(manifest.get("content_digest", "")):
                        raise ServiceError("invalid_backup", "Backup manifest digest does not match its content")
                    self._verify_archive(source, manifest)
                clean = str(project_id or manifest_id).strip()
                if not clean or not self.context.is_safe_project_id(clean) or clean == "default":
                    raise ServiceError("invalid_project_id", "Backup project_id is invalid")
                if manifest_id and manifest_id != clean:
                    raise ServiceError("invalid_backup", "Backup project_id does not match requested project")
                staging = self.context.ampuly_home / ".restore-staging" / uuid.uuid4().hex
                trees = self._restore_trees(clean, staging)
                expected_prefixes = tuple(prefix for _label, prefix, _staged, _target in trees)
                for name in names:
                    normalized = str(name).replace("\\", "/")
                    if normalized.startswith("/") or ".." in Path(normalized).parts:
                        raise ServiceError("invalid_backup", "Backup contains a traversal path")
                    if name != "manifest.json" and normalized not in FLAT_MEMBERS and not any(normalized.startswith(prefix) for prefix in expected_prefixes):
                        raise ServiceError("invalid_backup", "Backup contains an unexpected path")
                    if normalized.startswith("memory/") and normalized != f"memory/{clean}.md" \
                            and not normalized.startswith(f"memory/{clean}/customers/"):
                        raise ServiceError("invalid_backup", "Backup contains an unexpected memory path")
                    info = archive.getinfo(name)
                    unix_mode = (info.external_attr >> 16) & 0o170000
                    if unix_mode == 0o120000:
                        raise ServiceError("invalid_backup", "Backup contains a symbolic link")
                if any(target.exists() for _label, _prefix, _staged, target in trees):
                    raise ServiceError("project_exists", f"Project '{clean}' already exists")
                placed: list[Path] = []  # only what this restore put there is ever taken back out
                try:
                    try:
                        # By here the archive has been read and -- for an archive that carries digests -- every byte
                        # of it checked. What can still fail is the writing: a full disk, a scanner's lock, a path
                        # that is too long under the staging folder. That is not an unreadable backup.
                        staging.mkdir(parents=True, exist_ok=False)
                        archive.extractall(_staging_io_path(staging), members=[name for name in names if str(name).replace("\\", "/") != BUSINESS_RECORDS_MEMBER])
                        # Empty project/workplace directories are not represented
                        # as ZIP entries; recreate the project root before moving it.
                        staged_project = staging / "project" / clean
                        staged_project.mkdir(parents=True, exist_ok=True)
                        # 运行库的快照单独躺在 runtime/ 下面（它不是按文件拷进来的）。在项目树被挪到位
                        # **之前**把它放进去，这样它和三棵树是同一次成败——中途失败时一起被收走，不会留下
                        # 一个有任务库却没有项目的半成品。
                        staged_runtime = staging / "runtime" / "tasks.db"
                        if staged_runtime.is_file():
                            staged_runtime.replace(staged_project / "tasks.db")
                        # L4: open sealed CRM members before promote so the live tree is plaintext.
                        staged_crm = staged_project / "plugins" / "crm"
                        if staged_crm.is_dir():
                            from ampuly.plugins.crm_agent.backup_crypto import open_crm_tree_in_place
                            from ampuly.plugins.crm_agent.contracts import CRMError
                            try:
                                open_crm_tree_in_place(
                                    staged_crm, ampuly_home=self.context.ampuly_home, project_id=clean)
                            except CRMError as exc:
                                raise ServiceError(
                                    exc.code,
                                    f"CRM backup could not be opened: {exc.code}",
                                ) from exc
                        # A restored tree must already refuse sends when it first becomes visible.
                        # Hold failures leave every tree in staging, before any promotion occurs.
                        staged_service = staging / "customer_service" / clean
                        if staged_service.is_dir():
                            from ampuly.plugins.customer_service_console.restore_hold import hold_restored_sends
                            hold_restored_sends(staged_service)
                        for _label, _prefix, staged, target in trees:
                            if not staged.exists():
                                continue
                            target.parent.mkdir(parents=True, exist_ok=True)
                            self._promote(staged, target)
                            placed.append(target)
                    except OSError as exc:
                        # The archive was read and verified; what failed is putting it in place. Saying "could
                        # not read backup" here sent people looking for a fault in a good archive.
                        raise ServiceError("restore_failed", f"The backup is intact but its files could not be put in place: {exc}") from exc
                except Exception:
                    for target in placed:
                        if target.is_dir():
                            shutil.rmtree(str(target), ignore_errors=True)
                        elif target.exists():
                            target.unlink()
                    raise
                finally:
                    shutil.rmtree(_staging_io_path(staging), ignore_errors=True)
                    try:
                        # refuses a folder that is not empty: another restore may be using it right now. Looking
                        # first and removing second raced with exactly that, and the error it raised turned a
                        # restore that had fully succeeded into "could not read backup".
                        staging.parent.rmdir()
                    except OSError:
                        pass
        except ServiceError:
            raise
        except (OSError, zipfile.BadZipFile) as exc:
            raise ServiceError("invalid_backup", f"Could not read backup: {exc}") from exc
        return clean, source

    async def switch(self, project_id: str, *, switch_seq: str = "", include_snapshot: bool = True) -> ServiceResult:
        new_id = str(project_id or "").strip()
        if not new_id:
            raise ServiceError("missing_project_id", "Missing project_id")
        if not self.context.is_safe_project_id(new_id):
            raise ServiceError("invalid_project_id", "Invalid project_id")
        async with self.context.project_switch_lock:
            # Before anything is created. 「default」 used to mkdir its workplace two lines above this, which
            # manufactured the very folder the guard exists to find missing -- and 「default」 can be pinned like
            # any other project.
            pinned = self._owner_chose(new_id)
            if pinned is not None and not pinned.is_dir():
                # The disk is not plugged in, or the folder was moved. Switching anyway would hand the engine a
                # path it would then create, and an empty decoy would look like the project.
                raise ServiceError("workplace_missing", f"Project '{new_id}' is pointed at a folder that is not there",
                                   {"project_id": new_id, "switch_seq": switch_seq})
            if new_id == "default":
                self.context.project_dir(new_id).mkdir(parents=True, exist_ok=True)
                if pinned is None:
                    self.context.project_workplace(new_id).mkdir(parents=True, exist_ok=True)
            elif not self.context.project_dir(new_id).is_dir():
                raise ServiceError("project_not_found", f"Project '{new_id}' does not exist", {"project_id": new_id, "switch_seq": switch_seq})
            lifecycle = self._lifecycle(new_id)
            if lifecycle.status == "archived":
                raise ServiceError("project_archived", f"Project '{new_id}' is archived; restore it before switching", {"project_id": new_id})
            if lifecycle.status == "destroyed":
                raise ServiceError("project_destroyed", f"Project '{new_id}' is destroyed", {"project_id": new_id})
            engine = await self.context.activate_project(new_id)
            await self.context.chat_store.ensure_activity_channel(project_id=new_id)
            await self.context.chat_store.ensure_secretary_channel(project_id=new_id)

        events = [ServiceEvent("project_switched", {"project_id": new_id, "switch_seq": switch_seq})]
        if include_snapshot:
            index_payload = await self.project_index(new_id, switch_seq=switch_seq, include_snapshot=True)
            for key in ("project_index", "snapshot"):
                if key in index_payload.payload:
                    event_type = "project_index_push" if key == "project_index" else "snapshot"
                    events.append(ServiceEvent(event_type, index_payload.payload[key]))
        return ServiceResult({"project_id": new_id, "switch_seq": switch_seq, "engine_project_id": getattr(engine, "project_id", new_id)}, events)

    async def project_index(
        self,
        project_id: str,
        *,
        switch_seq: str = "",
        view_generation: Any = None,
        include_snapshot: bool = False,
    ) -> ServiceResult:
        engine = await self.context.engine_for_project(project_id)
        index_payload = await build_project_index_sync(
            engine,
            self.context.agent_store,
            self.context.chat_store,
            self.context.event_adapter,
            exec_mode=self.context.mode_state.exec_mode,
        )
        index_payload["project_id"] = self.context.normalize_project_id(project_id)
        index_payload["switch_seq"] = switch_seq
        if view_generation is not None:
            index_payload["view_generation"] = view_generation
        payload: dict[str, Any] = {"project_index": index_payload}
        if include_snapshot:
            snapshot = await build_snapshot(
                engine,
                self.context.agent_store,
                self.context.chat_store,
                self.context.event_adapter,
            )
            snapshot["project_id"] = self.context.normalize_project_id(project_id)
            snapshot["exec_mode"] = self.context.mode_state.exec_mode
            snapshot["company_profile"] = self.context.mode_state.company_profile
            snapshot["task_preferred_agent"] = self.context.mode_state.task_preferred_agent
            snapshot["switch_seq"] = switch_seq
            if view_generation is not None:
                snapshot["view_generation"] = view_generation
            payload["snapshot"] = snapshot
        return ServiceResult(payload)
