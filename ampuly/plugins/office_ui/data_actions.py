"""What the 「资料」 entry answers, built off the socket -- data phase 2, D6.

The seven websocket reads are thin: they resolve the project, build a viewer, and call one of the functions here.
Everything in this module is ordinary synchronous code with no socket, no engine and no event loop, so the handler
can run it in a thread and the tests can call it directly.

Three rules hold throughout:

* **Nothing is created.** No engine is built for the project (that would re-create a destroyed project's folder and
  an empty store), no folder is made, nothing is written. Every read goes through :mod:`data_browser`, which opens
  folders through ``confined_dir`` and files through the workspace capability.
* **One scope rule.** Who may see what is :mod:`data_scope` alone, asked with the same order map the reply is built
  from, so a listing never advertises what a download would refuse.
* **No absolute path leaves here**, except the owner-only ``data_locations``, which exists to answer "where does
  this live on my disk".
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ampuly.core.order_layout import (
    AUDIT,
    DELIVERY_EXPORTS,
    DELIVERY_HISTORY,
    DOWNLOADS,
    INPUTS,
    MATERIALS_COMMON,
    MATERIALS_CUSTOMERS,
    ORDERS,
    OUTPUT,
    PORTFOLIO_EXPORTS,
    PROJECT_MATERIALS,
    customer_segment,
)
from ampuly.plugins.office_ui import data_browser
from ampuly.plugins.office_ui.data_browser import Budget
from ampuly.plugins.office_ui.data_scope import Decision, Viewer, decide, path_parts, project_gate

# How much of the business store one request may read: the newest orders in one query, then a few lookups by name
# for a folder the query did not cover. Past that an unknown folder is "not an order of this project", which is the
# owner's view only and so fails closed for everyone else.
ORDER_ROWS = 500
ORDER_LOOKUPS = 50
OTHER_FOLDERS = 200
HISTORY_ROUNDS = 200
EXPORT_FILES = 100
PORTFOLIO_FILES = 100
MATERIAL_CUSTOMERS = 200

# A usage walk is expensive and never urgent: one answer is kept for ten minutes, and a refresh is honoured at most
# once a minute per project.
USAGE_TTL = 600.0
USAGE_COOLDOWN = 60.0


class DataDenied(Exception):
    """A read this viewer may not do. ``code`` is what the console shows; never a path, never OS text.

    ``project_id`` is carried so the refusal is recorded against the project that was asked for: an attempt on
    somebody else's delivery space belongs in that space's own trail, not in the default project's.
    """

    def __init__(self, code: str, project_id: str = "") -> None:
        super().__init__(code)
        self.code = str(code or "owner_only")
        self.project_id = str(project_id or "")


@dataclass(frozen=True)
class Roots:
    """Where this project's data sits. Absolute paths stay inside this object and ``locations``."""

    project: Path
    attachments: Path
    workplace: Path
    home: Path


class OrderLookup:
    """The orders of one project, as one query plus a few lookups by name.

    ``customer(order_id)`` is what :mod:`data_scope` asks: the order's customer, or ``None`` when this project has no
    such order -- a folder left by an older run, keyed by a task id, answers ``None``.
    """

    def __init__(self, business: Any, project_id: str, *, rows: int = ORDER_ROWS,
                 lookups: int = ORDER_LOOKUPS) -> None:
        self._business = business
        self._project_id = str(project_id)
        self._lookups = int(lookups)
        try:
            listed = list(business.list_orders(project_id=self._project_id, limit=int(rows)) or [])
        except Exception:
            # A store that cannot be read is not "no orders": every order folder would then look legacy, which is
            # owner-only, so the fail-closed side is the empty map plus a truncated flag.
            listed, self.store_failed = [], True
        else:
            self.store_failed = False
        self.orders: dict[str, Any] = {str(order.order_id): order for order in listed}
        self.truncated = len(listed) >= int(rows)
        self._memo: dict[str, Any] = {}

    def order(self, order_id: str) -> Any | None:
        clean = str(order_id or "").strip()
        if not clean:
            return None
        if clean in self.orders:
            return self.orders[clean]
        if clean in self._memo:
            return self._memo[clean]
        if self._lookups <= 0:
            return None
        self._lookups -= 1
        try:
            found = self._business.get_order(clean, project_id=self._project_id)
        except Exception:
            found = None
        self._memo[clean] = found
        return found

    def customer(self, order_id: str) -> str | None:
        order = self.order(order_id)
        if order is None:
            return None
        return str(getattr(order, "customer_id", "") or "").strip()

    def known(self, order_id: str) -> bool:
        return self.order(order_id) is not None


def order_row(order: Any, *, has_folder: bool | None = None) -> dict[str, Any]:
    """One order as the 资料 pages show it: what it is, not how it ran."""
    row = {
        "order_id": str(getattr(order, "order_id", "") or ""),
        "status": str(getattr(order, "status", "") or ""),
        "workflow_id": str(getattr(order, "workflow_id", "") or ""),
        "customer_id": str(getattr(order, "customer_id", "") or ""),
        "customer_name": str(getattr(order, "customer_name", "") or ""),
        "created_at": str(getattr(order, "created_at", "") or ""),
    }
    if has_folder is not None:
        row["has_folder"] = bool(has_folder)
    return row


def portfolio_segment(order_id: str) -> str:
    """The folder one order's portfolio exports sit in: the opaque reference ``portfolio_bundle`` writes."""
    return hashlib.sha256(str(order_id or "").encode("utf-8")).hexdigest()[:20]


def object_id_for(area: str, order_id: str, path: str, *, known_order: bool) -> str:
    """What an observation records as the object read.

    An owner-only area records the area alone, never a file name: an unbound employee reading the audit list then
    sees that the owner looked at an order's operation records, not which recording.
    """
    if area in {AUDIT, DOWNLOADS}:
        return f"{AUDIT}:{order_id}" if known_order and order_id else AUDIT
    if area == PORTFOLIO_EXPORTS:
        return PORTFOLIO_EXPORTS
    if order_id and not known_order:
        return "legacy_folder"
    return str(path or "")


@dataclass
class Request:
    """One 资料 read as the handler hands it to the work: the project, who is asking, and where its files are.

    The order map is built on first use, so an answer that needs no order (the disk locations, the attachments)
    makes no database call at all.
    """

    project_id: str
    viewer: Viewer
    roots: Roots
    project_customer: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    business: Any = None
    is_destroyed: Callable[[], bool] | None = None
    read_binding: Callable[[], str | None] | None = None
    _lookup: OrderLookup | None = None

    def open(self) -> None:
        """The checks that read the business store. Called in a thread, never on the socket's own loop.

        Opening a SQLite store replays its schema, and the console has one store for every project: doing that on
        the loop stalls every other client, every push and the file routes for as long as it takes -- and longer
        while a run holds the write lock.
        """
        if self.is_destroyed is not None:
            try:
                destroyed = bool(self.is_destroyed())
            except Exception:
                destroyed = False  # a lifecycle that cannot be read does not make a live project unreadable
            if destroyed:
                raise DataDenied("project_destroyed", self.project_id)
        if self.read_binding is not None:
            try:
                self.project_customer = self.read_binding()
            except Exception:
                # Fail closed for a bound account (the gate refuses an unreadable binding) and open for the owner,
                # who is not bound to anything. Refusing the owner here would make the download refuse exactly what
                # the listing had just shown.
                self.project_customer = None
        gate(self.viewer, self.project_customer, self.project_id)

    def lookup(self) -> OrderLookup:
        if self._lookup is None:
            self._lookup = OrderLookup(self.business, self.project_id)
        return self._lookup


def customer_of_material(segment: str, lookup: OrderLookup, project_customer: str | None = None) -> str:
    """The customer whose material sits in this folder, or ``""``.

    The folder is named by ``customer_segment()``, which is a digest for any id a file system would argue about, so
    the segment itself is not a customer id. Recording it as one filed the read under a customer nobody has, and the
    customer it really concerned never saw the row in their own trail.
    """
    clean = str(segment or "")
    if not clean or clean == MATERIALS_COMMON:
        return ""
    known = {str(getattr(order, "customer_id", "") or "").strip() for order in lookup.orders.values()}
    if project_customer:
        known.add(str(project_customer).strip())
    for candidate in known:
        if candidate and customer_segment(candidate) == clean:
            return candidate
    return ""


def allow_rule(viewer: Viewer, lookup: OrderLookup) -> Callable[[tuple[str, ...]], bool]:
    """The listing's own filter: one entry, one scope answer, no disk access."""
    return lambda parts: decide(viewer, "/".join(parts), lookup.customer).allowed


def gate(viewer: Viewer, project_customer: str | None, project_id: str = "") -> None:
    """Refuse a viewer who may not be in this project at all."""
    reason = project_gate(viewer, project_customer)
    if reason:
        raise DataDenied("customer_scope_denied", project_id)


# ── The seven answers ────────────────────────────────────────────────────────────────────────────────────────────

def project_index(roots: Roots, viewer: Viewer, lookup: OrderLookup, *, project_customer: str | None = None,
                  usage: dict[str, Any] | None = None) -> dict[str, Any]:
    """The entry's own page: this project's orders, its material area, its attachments, its size when known."""
    root = roots.project
    budget = Budget(entries=data_browser.ORDER_VIEW_ENTRIES, seconds=data_browser.ORDER_VIEW_SECONDS)
    orders: list[dict[str, Any]] = []
    customers: list[str] = []
    for order in lookup.orders.values():
        customer = str(getattr(order, "customer_id", "") or "").strip()
        if viewer.bound and not viewer.has_customer(customer):
            continue
        if customer:
            customers.append(customer)
        orders.append(order_row(order, has_folder=data_browser.exists(
            root, (ORDERS, str(order.order_id)), budget=budget)))
    orders.sort(key=lambda row: (row.get("created_at", ""), row.get("order_id", "")), reverse=True)

    other_folders: list[str] = []
    if viewer.owner:
        listed = data_browser.list_dir(root, (ORDERS,), page=data_browser.ORDER_FOLDERS)
        for entry in listed.get("entries", []) or []:
            if entry.get("kind") != "dir" or str(entry.get("name", "")) in lookup.orders:
                continue
            if lookup.known(str(entry.get("name", ""))):
                continue
            other_folders.append(str(entry.get("name", "")))
            if len(other_folders) >= OTHER_FOLDERS:
                break

    if viewer.bound:
        material_customers = list(viewer.customers)
    else:
        material_customers = list(dict.fromkeys(
            ([str(project_customer).strip()] if project_customer else []) + customers))
    material_rows = [
        {"customer_id": customer, "segment": customer_segment(customer),
         "exists": data_browser.exists(root, (PROJECT_MATERIALS, MATERIALS_CUSTOMERS, customer_segment(customer)))}
        for customer in material_customers[:MATERIAL_CUSTOMERS]
    ]
    loose = None
    if not viewer.bound:
        counted = data_browser.count_dir(root, (PROJECT_MATERIALS,))
        loose = {"files": counted.get("files"), "folders": counted.get("folders")}
    materials = {
        "exists": data_browser.exists(root, (PROJECT_MATERIALS,)),
        "common": data_browser.exists(root, (PROJECT_MATERIALS, MATERIALS_COMMON)),
        "customers": material_rows,
        "loose": loose,
    }

    uploaded = data_browser.attachment_page(roots.attachments, page=1)
    return {
        "viewer": {"owner": bool(viewer.owner), "bound": bool(viewer.bound)},
        "exists": data_browser.exists(root, ()),
        "orders": orders,
        "orders_truncated": bool(lookup.truncated),
        "orders_unavailable": bool(lookup.store_failed),
        "other_order_folders": other_folders,
        "materials": materials,
        "attachments": {"count": int(uploaded.get("total", 0) or 0),
                        "truncated": bool(uploaded.get("truncated"))},
        "usage": usage,
    }


def folder_list(roots: Roots, viewer: Viewer, lookup: OrderLookup, folder: str, offset: int = 0) -> dict[str, Any]:
    """One page of one folder, filtered to what this viewer may see."""
    # Not stripped: the scope decides what a noncanonical spelling is, so the listing refuses exactly what a
    # download would refuse.
    clean = str(folder or "")
    if clean:
        answer = decide(viewer, clean, lookup.customer)
        if not answer.allowed:
            raise DataDenied(_scope_code(answer.reason))
        parts = path_parts(clean)
        area, order_id, customer_id = answer.area, answer.order_id, answer.customer_id
    else:
        parts, area, order_id, customer_id = (), "", "", ""
    start = max(0, int(offset or 0))
    page = data_browser.list_dir(roots.project, parts, allow=allow_rule(viewer, lookup), offset=start)
    # The page says which offset it answered: a later page is appended to the one before it, never to a page the
    # console had already replaced.
    return {"folder": "/".join(parts), "area": area, "order_id": order_id, "customer_id": customer_id,
            "label": _label(parts, area), "offset": start, **page}


def preview_file(roots: Roots, viewer: Viewer, lookup: OrderLookup, path: str) -> dict[str, Any]:
    """What one file's page shows. The scope answer comes first, then the checked read."""
    answer = decide(viewer, str(path or ""), lookup.customer)
    if not answer.allowed:
        raise DataDenied(_scope_code(answer.reason))
    payload = data_browser.preview(roots.project, str(path or ""))
    return {**payload, "area": answer.area, "order_id": answer.order_id, "customer_id": answer.customer_id}


def order_view(roots: Roots, viewer: Viewer, lookup: OrderLookup, order_id: str) -> dict[str, Any]:
    """One order's page: what it delivered, step by step, and where the rest of its files are."""
    clean = str(order_id or "").strip()
    answer = decide(viewer, f"{ORDERS}/{clean}", lookup.customer)
    if not clean or not answer.allowed:
        raise DataDenied(_scope_code(answer.reason) if clean else "invalid_path")
    order = lookup.order(clean)
    budget = Budget(entries=data_browser.ORDER_VIEW_ENTRIES, seconds=data_browser.ORDER_VIEW_SECONDS)
    table = data_browser.read_step_table(roots.project, clean)
    view = data_browser.order_view(roots.project, clean, table, budget=budget)
    areas = [area for area in view.get("areas", []) or []
             if decide(viewer, str(area.get("path", "") or ""), lookup.customer).allowed]
    history = data_browser.list_dir(roots.project, (DELIVERY_HISTORY, clean), page=HISTORY_ROUNDS)
    exports = data_browser.list_dir(roots.project, (DELIVERY_EXPORTS, clean), page=EXPORT_FILES)
    payload: dict[str, Any] = {
        "kind": "order" if order is not None else "legacy_folder",
        "order": order_row(order) if order is not None else {"order_id": clean},
        "step_table": {"state": table.get("state", ""), "dropped": int(table.get("dropped", 0) or 0)},
        "steps": view.get("steps", []),
        "areas": [{**area, "label": _AREA_LABELS.get(str(area.get("area", "") or ""), "")} for area in areas],
        "counts_complete": bool(view.get("counts_complete")),
        "history": {"path": f"{DELIVERY_HISTORY}/{clean}", "rounds": history.get("entries", []),
                    "exists": bool(history.get("exists"))},
        "exports": {"path": f"{DELIVERY_EXPORTS}/{clean}", "files": exports.get("entries", []),
                    "exists": bool(exports.get("exists"))},
        "customer_id": answer.customer_id,
    }
    if viewer.owner:
        segment = portfolio_segment(clean)
        portfolio = data_browser.list_dir(roots.project, (PORTFOLIO_EXPORTS, segment), page=PORTFOLIO_FILES)
        payload["portfolio"] = {"path": f"{PORTFOLIO_EXPORTS}/{segment}", "files": portfolio.get("entries", []),
                                "exists": bool(portfolio.get("exists"))}
    return payload


def attachments(roots: Roots, viewer: Viewer, *, offset: int = 0) -> dict[str, Any]:
    """What was uploaded to this project. The files themselves are served by the existing attachment route."""
    if viewer.bound:
        # An upload carries no customer of its own, so there is no per-record answer for a bound account yet.
        raise DataDenied("owner_only")
    start = max(0, int(offset or 0))
    page = data_browser.attachment_page(roots.attachments, offset=start)
    return {"exists": bool(page.get("exists")), "attachments": page.get("entries", []), "offset": start,
            "next_offset": int(page.get("next_offset", 0) or 0), "truncated": bool(page.get("truncated")),
            "total": int(page.get("total", 0) or 0)}


# 数据与文件 P3: 这张卡片上每一行的 key 与顺序，配的是 core.data_map 里的哪一条。
#
# key 保持历史写法（前端与测试按它认行），而位置、说法、归谁、留多久、进不进备份全部来自那份声明——这张卡片
# 从此不再是第五份抄件。名字对不上的那几个（global_tasks_db / backups / logs）是控制台的旧称，留着不动。
_LOCATION_ROWS: tuple[tuple[str, str], ...] = (
    ("ampuly_home", "ampuly_home"),
    ("workplace_root", "workplace_root"),
    ("project_workplace", "project_workplace"),
    ("project_dir", "project_dir"),
    ("attachments", "attachments"),
    ("project_memory", "project_memory"),
    ("customer_memory", "customer_memory"),
    ("business_db", "business_db"),
    ("ui_state_db", "ui_state_db"),
    ("project_tasks_db", "project_tasks_db"),
    ("global_tasks_db", "global_db"),
    ("exports", "exports"),
    ("backups", "project_backups"),
    ("logs", "runtime_logs"),
    ("external_agent_logs", "external_agent_logs"),
    ("browser_artifacts", "browser_artifacts"),
    ("node_trials", "node_trials"),
    ("browser_engine", "browser_engine"),
)


def locations(roots: Roots, project_id: str) -> list[dict[str, str | bool]]:
    """Where this project's data is on disk, for the owner who asked. The only reply that carries absolute paths.

    Every row also says what kind of data it is, how long it is kept and whether a backup carries it -- read off
    :mod:`ampuly.core.data_map`, which is the one place that decides. Before P3 this list was thirteen hard-coded
    lines that had already drifted apart from the backup and from the restore notes.
    """
    from ampuly.core.data_map import BY_KEY, Paths, path_of

    pid = str(project_id)
    paths = Paths(home=roots.home, workplace_root=Path(roots.workplace),
                  project_workplace=Path(roots.project), project_dir=roots.home / "projects" / pid)
    out: list[dict[str, str | bool]] = []
    for shown, key in _LOCATION_ROWS:
        entry = BY_KEY[key]
        path = roots.attachments if key == "attachments" else path_of(entry, paths, pid)
        try:
            there = Path(path).exists()
        except OSError:
            there = False
        out.append({"key": shown, "label": entry.label_zh, "path": str(path), "exists": there,
                    "note": entry.note, "kind": entry.kind, "retention": entry.retention,
                    "in_backup": entry.in_backup, "reason": entry.reason})
    return out


def inside_system_dir(workplace: Path) -> bool:
    """Whether the work area sits under a folder Windows manages for the account.

    A synchronised folder (OneDrive, 文档, 桌面) copies every delivered file to somebody else's server and locks
    files while it uploads. The console says so; moving the area is phase 3 and the owner's decision.
    """
    try:
        parts = [part.casefold() for part in Path(workplace).resolve().parts]
    except OSError:
        return False
    watched = {"onedrive", "documents", "desktop", "downloads", "文档", "桌面", "下载"}
    return any(part in watched or part.startswith("onedrive") for part in parts)


def locations_payload(roots: Roots, project_id: str) -> dict[str, Any]:
    """The 真实位置 answer: where everything is, and what the console is obliged to say about it.

    It is the one reply that carries absolute paths, and the one place the owner is told that a folder they chose
    was refused, that a pinned folder is missing, or that the files they left behind are not in 资料.
    """
    from ampuly.core import dead_space, project_paths
    from ampuly.database import schema_version

    home = Path(roots.home)
    project_paths.invalidate(home)
    found = project_paths.mapping(home)
    pinned = found.pinned(str(project_id))
    # A pin of <our own root>/<pid> is the product recording where it already put things -- every project gets one
    # the moment a new default root is set -- not the owner naming a folder. Saying 「由你指定」 for a folder
    # inside our own home would be telling them something that is not so.
    chosen = pinned if (pinned is not None
                        and not project_paths.product_owned(home, str(project_id), pinned)) else None
    env_root = os.environ.get("AMPULY_WORKPLACE_ROOT")
    # Where the project is *now*: a reply that changed the folder would otherwise describe where it was when the
    # request arrived, and the panel would show the old path beside the words 「由你指定」.
    here = Roots(project=Path(pinned) if pinned is not None else Path(roots.workplace) / str(project_id),
                 attachments=roots.attachments, workplace=roots.workplace, home=roots.home)
    rows = locations(here, project_id)
    rows.insert(2, {"key": "default_root", "label": "新项目放在哪", "path": str(roots.workplace),
                    "exists": Path(roots.workplace).exists(), "note": "以后新建的项目都放到这里"})
    legacy = found.legacy.get(str(project_id))
    legacy_row = None
    if legacy is not None and Path(legacy).is_dir():
        counted = data_browser.count_dir(Path(legacy), ())
        legacy_row = {"path": str(legacy), "files": counted.get("files"), "folders": counted.get("folders")}
    return {
        "locations": rows,
        "workplace_set_by": "AMPULY_WORKPLACE_ROOT" if env_root else "default",
        "data_area_inside_system_dir": inside_system_dir(roots.workplace),
        "project_root_source": "project" if chosen is not None else "default",
        "default_root_set_by": ("AMPULY_WORKPLACE_ROOT" if env_root else
                                ("console" if found.default_root is not None else "default")),
        # The owner's ruling: the environment variable wins. When both are set, say so rather than showing a
        # setting that is recorded and not in force.
        "default_root_shadowed": bool(env_root and found.default_root is not None),
        "project_root_missing": bool(chosen is not None and not Path(chosen).is_dir()),
        "legacy_folder": legacy_row,
        "dropped_entries": [{"project_id": pid, "code": code} for pid, code in found.dropped],
        "later": [
            {"key": "backup_list", "phase": 3, "where": "备份与清理（第 3 期）"},
            {"key": "cleanup_rules", "phase": 3, "where": "过期文件规则（第 3 期）"},
        ],
        # 删掉的东西腾出来、却还没还给磁盘的那部分。只读的一次测量。
        # 这个仓库里真实发生过「428 MB 的库只有 19 MB 是活的」，而没有任何东西会在它变严重时出声——
        # 回收要停一次控制台，所以更需要有人盯着它什么时候才值得停。
        "dead_space": dead_space.dead_space(home),
        # 4.7-A19：每个运行库在表结构第几版。还没有版本号的库开机只读、不改，这里告诉 owner 该运行哪条命令。只读。
        "schema_versions": schema_version.health(home),
    }


_AREA_LABELS = {
    OUTPUT: "交付（全部）",
    INPUTS: "输入",
    AUDIT: "操作记录",
    DOWNLOADS: "下载（旧）",
    DELIVERY_HISTORY: "交付历史",
    DELIVERY_EXPORTS: "交付打包",
    PORTFOLIO_EXPORTS: "作品集打包",
    PROJECT_MATERIALS: "资料",
    ORDERS: "订单",
}


def backups(roots: Roots, project_id: str, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """这台机器上的备份列表（数据与文件 P3）。只读：不建、不删、不还原。"""
    from ampuly.plugins.office_ui import backup_list

    return backup_list.list_backups(roots.home, project_id=str(project_id or ""), offset=offset, limit=limit)


def cleanup_preview(roots: Roots, project_id: str) -> dict[str, Any]:
    """「看看会删掉什么」。**只列不删**：这一期不提供执行的通路（数据与文件 P3）。"""
    from ampuly.core.data_map import Paths
    from ampuly.plugins.office_ui import cleanup_plan

    paths = Paths(home=roots.home, workplace_root=Path(roots.workplace),
                  project_workplace=Path(roots.project), project_dir=roots.home / "projects" / str(project_id))
    return cleanup_plan.plan_cleanup(paths, project_id=str(project_id or ""))


def _label(parts: tuple[str, ...], area: str) -> str:
    if not parts:
        return "项目根目录"
    return _AREA_LABELS.get(area, "") or _AREA_LABELS.get(parts[0], "")


def refusal_for(answer: Decision, project_id: str = "") -> DataDenied:
    """The refusal one scope answer deserves, as the code the console shows."""
    return DataDenied(_scope_code(answer.reason), project_id)


def _scope_code(reason: str) -> str:
    """The scope's own reason, as one of the codes the console knows."""
    if reason in {"owner_only", "not_served"}:
        return "owner_only"
    if reason in {"not_your_customer", "project_not_yours", "project_scope_unknown"}:
        return "customer_scope_denied"
    return "invalid_path"


# ── The usage walk, off the socket's loop ────────────────────────────────────────────────────────────────────────

@dataclass
class _Walk:
    """One project's last usage answer, and the walk that is producing the next one."""

    result: dict[str, Any] | None = None
    computed_at: float = 0.0
    started_at: float = 0.0
    task: Any = None
    stop: bool = False


class UsageCache:
    """How much space each project takes, computed at most once at a time and never on the socket's loop.

    A walk is expensive; the answer is not urgent. So a fresh answer is given at once, a stale one is given with the
    walk started behind it, and a caller who asks again while it runs waits for nothing. Nothing is persisted: on a
    restart the first ask walks again.
    """

    def __init__(self, *, ttl: float = USAGE_TTL, cooldown: float = USAGE_COOLDOWN) -> None:
        self._ttl = float(ttl)
        self._cooldown = float(cooldown)
        self._walks: dict[str, _Walk] = {}
        self._one_at_a_time = asyncio.Semaphore(1)
        self._closed = False

    def cached(self, project_id: str) -> dict[str, Any] | None:
        walk = self._walks.get(str(project_id))
        return walk.result if walk else None

    def fresh(self, project_id: str, *, now: float | None = None) -> bool:
        walk = self._walks.get(str(project_id))
        if walk is None or walk.result is None:
            return False
        return (now or time.monotonic()) - walk.computed_at < self._ttl

    def computing(self, project_id: str) -> bool:
        walk = self._walks.get(str(project_id))
        return bool(walk and walk.task is not None and not walk.task.done())

    def refresh_after(self, project_id: str, *, now: float | None = None) -> float:
        """Seconds until a refresh would be honoured again; 0 when it would be honoured now."""
        walk = self._walks.get(str(project_id))
        if walk is None:
            return 0.0
        waited = (now or time.monotonic()) - max(walk.started_at, walk.computed_at)
        return max(0.0, round(self._cooldown - waited, 3))

    def start(self, project_id: str, walk: Callable[[Callable[[], bool]], dict[str, Any]],
              *, on_done: Callable[[dict[str, Any] | None], Any] | None = None) -> bool:
        """Start one walk behind the answer that is being sent; False when one is already running or closed."""
        pid = str(project_id)
        entry = self._walks.setdefault(pid, _Walk())
        if self._closed or (entry.task is not None and not entry.task.done()):
            return False
        entry.stop = False
        entry.started_at = time.monotonic()
        entry.task = asyncio.ensure_future(self._run(pid, entry, walk, on_done))
        return True

    async def _run(self, project_id: str, entry: _Walk,
                   walk: Callable[[Callable[[], bool]], dict[str, Any]],
                   on_done: Callable[[dict[str, Any] | None], Any] | None) -> None:
        result: dict[str, Any] | None = None
        try:
            async with self._one_at_a_time:
                if entry.stop or self._closed:
                    return
                result = await asyncio.to_thread(walk, lambda: entry.stop or self._closed)
                entry.result = result
                entry.computed_at = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            result = None
        finally:
            entry.task = None
        if on_done is not None:
            try:
                outcome = on_done(result)
                if asyncio.iscoroutine(outcome):
                    await outcome
            except Exception:
                pass

    def close(self) -> None:
        """Stop every walk: the flag first, so a walk in a thread ends by itself, then the task."""
        self._closed = True
        for entry in self._walks.values():
            entry.stop = True
            task = entry.task
            if task is not None and not task.done():
                task.cancel()


def usage_walk(roots: Roots, project_id: str, *, zip_roots: Iterable[tuple[Path, str]] | None = None,
               stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    """One usage answer, as a plain function a thread can run."""
    budget = Budget(entries=data_browser.USAGE_ENTRIES, seconds=data_browser.USAGE_SECONDS, stop=stop)
    zips = list(zip_roots) if zip_roots is not None else [
        (roots.home / "exports", str(project_id)), (roots.home / "backups" / "projects", str(project_id))]
    started = time.monotonic()
    payload = data_browser.project_usage(roots.project, roots.attachments, zip_roots=zips, budget=budget)
    # A number kept for ten minutes has to say when it was true, or a reader takes it for "right now" -- which it is
    # not once files have been added or removed since.
    return {**payload, "elapsed_ms": int((time.monotonic() - started) * 1000),
            "computed_at": datetime.now().isoformat(timespec="seconds")}
