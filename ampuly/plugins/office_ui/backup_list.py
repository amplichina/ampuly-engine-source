"""控制台里看得见的备份列表（数据与文件 第 3 期）。只读：这个模块不建、不删、不还原任何备份。

在这之前，备份是看不见的。要还原得手打路径，而唯一的提示词只活在当前这一次会话里——刷新一下就没了。一份
备份存不存在、装了什么、还原得了还原不了，都只有打开压缩包才知道。

目录本身就是列表，不需要建表：文件名 ``<项目>-<时间戳>.zip`` 是打包那一侧保证的。每一行回答四件事——**什么
时候备的、里面有什么、多大、能不能还原（不能的话为什么）**。

坏掉的那一份照样出现在列表里，标着坏掉。它恰恰是你最需要看见的那一份：一个读不出来的备份，在列表上消失和
在列表上标红，是完全不同的两件事。

只读 ``manifest.json`` 一个成员，不逐成员算哈希——那是「检查这一份」按钮的活，一次一份，由人点。
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

MANIFEST_NAME = "manifest.json"
# 打包那一侧的文件名格式（services/project.py 的 export）。认不出名字的文件不当成备份。
_NAME = re.compile(r"^(?P<project>.+)-(?P<stamp>\d{8}T\d{12})\.zip$")
LIST_LIMIT = 50


@dataclass(frozen=True)
class Backup:
    """一份备份，按列表需要的样子。"""

    name: str
    path: Path
    project_id: str
    taken_at: str
    bytes: int


def backups_root(home: Path) -> Path:
    return Path(home) / "backups" / "projects"


def _human_stamp(stamp: str) -> str:
    """``20260924T005306206356`` → ``2026-09-24 00:53:06``。认不出来就原样返回。"""
    text = str(stamp or "")
    if len(text) < 15 or text[8] != "T":
        return text
    return f"{text[0:4]}-{text[4:6]}-{text[6:8]} {text[9:11]}:{text[11:13]}:{text[13:15]}"


def read_manifest(path: Path) -> dict[str, Any] | None:
    """这一份备份的清单，读不出来就是 None（坏了、不是 zip、没有清单成员）。"""
    try:
        with zipfile.ZipFile(path) as archive:
            return json.loads(archive.read(MANIFEST_NAME))
    except Exception as exc:  # noqa: BLE001 - 读不出来的备份仍然要出现在列表里，标着读不出来
        # 名字和原因，不带堆栈：一份坏掉的备份是预期情况，不是内部错误，不该在日志里刷一屏。
        logger.debug("backup list: {} 的清单读不出来（{}）", Path(path).name, type(exc).__name__)
        return None


def _holds(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """这一份里装了什么，说给人听。来自备份自己的 coverage，没有 coverage 就说不出（旧版本）。"""
    from ampuly.core.data_map import BY_KEY

    coverage = manifest.get("coverage")
    if not isinstance(coverage, dict):
        return []
    extra = {"budgets": "预算与预留", "project_history": "项目自己的生命周期记录", "chat": "聊天记录"}
    out: list[dict[str, Any]] = []
    for key, state in coverage.items():
        if not isinstance(state, dict) or str(state.get("state", "")) != "included":
            continue
        entry = BY_KEY.get(key)
        label = entry.label_zh if entry is not None else extra.get(key, key)
        counts = {str(k): int(v) for k, v in dict(state.get("counts", {}) or {}).items() if int(v or 0)}
        out.append({"key": key, "label": label, "files": int(state.get("files", 0) or 0),
                    "counts": counts, "restored": state.get("restored", True)})
    return sorted(out, key=lambda row: str(row["label"]))


def backup_row(path: Path, *, home: Path) -> dict[str, Any]:
    """列表里的一行。坏掉的那一份也有一行，标着 ``blocked``。"""
    stat = None
    try:
        stat = path.stat()
    except OSError:
        pass
    matched = _NAME.match(path.name)
    row: dict[str, Any] = {
        "name": path.name,
        "project_id": matched.group("project") if matched else "",
        "taken_at": _human_stamp(matched.group("stamp")) if matched else "",
        "bytes": int(stat.st_size) if stat is not None else 0,
        "entries": 0,
        "schema_version": 0,
        "holds": [],
        "restorable": False,
        "note": "",
        "blocked": False,
    }
    manifest = read_manifest(path)
    if manifest is None:
        row["blocked"] = True
        row["note"] = "读不出这份备份的清单，它可能已经损坏"
        return row
    row["schema_version"] = int(manifest.get("schema_version", 0) or 0)
    row["entries"] = len(list(manifest.get("files", []) or []))
    row["project_id"] = str(manifest.get("project_id", "") or row["project_id"])
    row["taken_at"] = _human_stamp(str(manifest.get("created_at", "") or "")) or row["taken_at"]
    row["holds"] = _holds(manifest)
    # 还原今天只从备份目录读，而且拒绝叫 default 的项目（services/project.py、restore_preflight）。
    # 把「为什么不能还原」直接写在行上，比让人点进去撞一次墙好。
    if str(row["project_id"]) == "default":
        row["note"] = "默认项目的名字不能被还原：可以备份，但产品还不能把它放回去"
    elif backups_root(home).resolve() not in Path(path).resolve().parents:
        row["note"] = "这份文件不在备份目录里，还原只从那里读"
    else:
        row["restorable"] = True
    return row


def list_backups(home: Path, *, project_id: str = "", offset: int = 0,
                 limit: int = LIST_LIMIT) -> dict[str, Any]:
    """备份列表，最近的在前。目录就是列表，不建表。"""
    root = backups_root(home)
    try:
        found = [item for item in root.iterdir() if item.is_file() and item.suffix.lower() == ".zip"]
    except OSError:
        found = []
    wanted = str(project_id or "").strip()
    if wanted:
        found = [item for item in found if (_NAME.match(item.name) or None) and _NAME.match(item.name).group("project") == wanted]
    found.sort(key=lambda item: item.name, reverse=True)
    start = max(0, int(offset or 0))
    size = max(1, min(int(limit or LIST_LIMIT), LIST_LIMIT))
    page = found[start:start + size]
    from ampuly.core import scheduled_task
    from ampuly.plugins.office_ui import backup_health

    return {
        "root": str(root),
        "total": len(found),
        "offset": start,
        "limit": size,
        "backups": [backup_row(item, home=Path(home)) for item in page],
        # 这张卡片上最该有、以前却没有的一句话：自动备份上次跑得怎么样，**备的是哪个家目录**。
        # 只列「有哪些备份文件」看不出「自动备份其实在备别人的现场」——那正是 2026-09-24 的事故。
        "auto": backup_health.auto_backup_health(Path(home)),
        # 这台机器上装过的备份计划任务，**连别的安装装的也一起列**。光看任务名看不出问题——2026-09-24
        # 那个每晚成功却备着另一个仓库的任务，名字是对的。所以这里显示的是每个任务保护的那个家目录。
        "tasks": scheduled_task.product_tasks(Path(home)),
        # 这些备份一共占了多少。项目备份是用户材料，按 owner 的裁决永久保留、只有你自己能删——所以这里
        # 不替你删，只让你看得见它有多大。
        "bytes": sum((item.stat().st_size for item in found if item.is_file()), 0),
    }
