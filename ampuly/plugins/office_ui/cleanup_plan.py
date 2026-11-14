"""「看看会删掉什么」——清理计划，只列不删（数据与文件 第 3 期）。

**这个模块就是这一期的停止线。** 它走一遍产品自己那三个目录，按 :mod:`ampuly.core.retention` 的四道闸逐个判定，
把「够 30 天、可以清理」的那些列出来给你看。它不删任何东西，这一期也不会有 ``apply_cleanup``：真正删除过期
文件是 owner 点名要先报范围、等一句确认的时刻之一。有一条源码扫描测试钉住这一点。

计划里带一个**内容令牌**，算法和 :mod:`folder_move` 那套「先计划、动手前重新计划、令牌变了就拒绝」一样。这一
期没有「动手」那一步，但令牌现在就存在——等到真要删的那一批，它绕不过去。

计划还会告诉你**闸拦下了多少**。只看「要删 12 项」，你无从判断这条规则是不是把该留的也算进去了；看到「拦下
37 项用户材料、9 项不认识的」，那条规则才是可检验的。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from ampuly.core import retention
from ampuly.core.data_map import BY_KEY, Paths

SAMPLE_LIMIT = 50
# 一次预览最多看多少项。走的是产品自己那三个目录，正常远到不了；到了就如实说「没看完」。
SCAN_LIMIT = 20000


@dataclass
class FamilyPlan:
    """一族里将要被清理的东西。"""

    key: str
    label_zh: str
    root: str
    unit_zh: str
    items: int = 0
    files: int = 0
    bytes: int = 0
    oldest: str = ""
    newest: str = ""
    sample: list[dict[str, Any]] = field(default_factory=list)


def _count_tree(root: Path) -> tuple[int, int]:
    """这一项里有多少文件、多少字节。**不跟随链接**，也不跨出这一项。"""
    files = 0
    total = 0
    try:
        if root.is_file():
            return 1, root.stat().st_size
        for here, dirs, names in os.walk(root, followlinks=False):
            for name in names:
                try:
                    total += (Path(here) / name).stat().st_size
                    files += 1
                except OSError:
                    files += 1
    except OSError:
        pass
    return files, total


def _modified(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def _iso(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M") if moment is not None else ""


def plan_cleanup(paths: Paths, *, project_id: str = "", now: datetime | None = None,
                 keep_days: int = retention.KEEP_DAYS, limit: int = SCAN_LIMIT) -> dict[str, Any]:
    """将要清理什么，一项都不删。

    只走产品自己那三个目录的第一层：可清理的单位本来就只有那一层（一次试跑、一天的日志、一份候选），往下走
    既没有别的单位，也会把时间花在用户的文件上——而那些本来就一个都不该动。
    """
    moment = now or datetime.now()
    plans: list[FamilyPlan] = []
    kept: dict[str, int] = {}
    fingerprint = hashlib.sha256()
    seen = 0
    incomplete = False

    for family in retention.FAMILIES:
        root = retention.family_root(family, paths, project_id)
        entry = BY_KEY[family.key]
        plan = FamilyPlan(key=family.key, label_zh=entry.label_zh, root=str(root), unit_zh=family.unit_zh)
        try:
            children = sorted(root.iterdir()) if root.is_dir() else []
        except OSError:
            children = []
        for child in children:
            if seen >= max(1, int(limit)):
                incomplete = True
                break
            seen += 1
            modified = _modified(child)
            judged = retention.classify(child, paths, project_id=project_id, modified=modified,
                                        now=moment, keep_days=keep_days)
            if judged.verdict != retention.CLEANABLE:
                kept[judged.verdict] = kept.get(judged.verdict, 0) + 1
                continue
            files, size = _count_tree(child)
            plan.items += 1
            plan.files += files
            plan.bytes += size
            stamp = _iso(modified)
            plan.oldest = min(filter(None, (plan.oldest, stamp)), default="")
            plan.newest = max(plan.newest, stamp)
            fingerprint.update(f"{child.name}\x1f{size}\x1f{stamp}".encode("utf-8"))
            if len(plan.sample) < SAMPLE_LIMIT:
                # 只给名字，不给绝对路径：这张卡片说的是「哪一天的日志」，不是「机器上的哪条路径」。
                plan.sample.append({"name": child.name, "files": files, "bytes": size, "modified": stamp})
        if plan.items or plan.sample:
            plans.append(plan)
        else:
            plans.append(plan)   # 空的那一族也列出来：让你看见它被看过了，而不是被忘了

    logger.debug("cleanup preview: {} families, {} scanned", len(plans), seen)
    return {
        "families": [vars(plan) for plan in plans],
        "totals": {"items": sum(p.items for p in plans), "files": sum(p.files for p in plans),
                   "bytes": sum(p.bytes for p in plans)},
        # 闸拦下了什么。只看「要删 12 项」无从判断规则对不对；看见「拦下多少、为什么」才是可检验的。
        "kept": {"protected": kept.get(retention.PROTECTED, 0),
                 "too_young": kept.get(retention.KEPT_TOO_YOUNG, 0),
                 "unknown": kept.get(retention.KEPT_UNKNOWN, 0)},
        "keep_days": int(keep_days),
        "made_at": moment.strftime("%Y-%m-%d %H:%M:%S"),
        "scanned": seen,
        "incomplete": incomplete,
        # 内容令牌：真要删的那一批必须先重新计划、比对令牌，变了就拒绝。这一期没有「删」，令牌先立在这里。
        "token": fingerprint.hexdigest()[:32],
        # 说给人听的那句话，和规则本身来自同一处声明，不另写一遍。
        "never_removed": [BY_KEY[key].label_zh for key in
                          ("project_workplace", "attachments", "customer_memory", "project_memory",
                           "project_backups", "exports", "browser_artifacts", "external_agent_logs")],
    }
