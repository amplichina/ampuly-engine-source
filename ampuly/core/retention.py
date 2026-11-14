"""什么可以自动清理，什么永远不行 —— 四道闸，默认全拒（数据与文件 第 3 期）。

owner 的裁决只有两句话：**涉及用户订单历史和资料的一律永久保留，删不删由用户自己决定，系统不自动删除**；
只有产品自己的东西自动清理，保留 30 天，恰好三族——搭建台试跑记录、系统运行日志、临时中转文件。

把这两句话写成代码有两种写法。一种是列一张「可以删什么」的模式清单——那张清单每长一条，删错东西的可能就多
一分，而且没有人会在加第 47 条时重新想一遍。这里用的是另一种：

1. **先问保护。** 路径落在任何一个保护根之下，立刻判「保护」，后面的规则没有机会推翻它。保护根由
   :mod:`ampuly.core.data_map` 里所有 ``user_material`` 的条目**自动生成**，不是手抄的——新加一类用户材料，
   保护自动跟上。
2. **再问三族。** :data:`FAMILIES` 恰好三条，和裁决一一对应。而且不是「在这个目录下面就行」，每一族还要说清
   **什么形状的东西**才是一个可清理的单位：日志只认 ``logs/<日期>/`` 这样的日目录，所以 ``logs/external_agents``
   （里面可能夹带订单内容）压根不会被选中。
3. **再自我断言。** 返回「可清理」之前，函数自己检查这条路径确实在该族根之下；不成立就抛异常。**这一道是防
   自己人**：将来谁把规则改歪了，炸掉的是清理器，不是用户的文件。
4. **最后问年龄。** 不满 30 天一律保留。

外加两条兜底：:func:`assert_families_cannot_select_protected` 在导入时跑一次，证明三族的选取规则不可能选中任何
一个保护根；以及——**认不出来的一律保留**。不知道的东西永远归用户。

纯函数，不碰磁盘（年龄由调用方把修改时间传进来），不删任何东西。这个模块里没有、也不会有 ``unlink``。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from ampuly.core.data_map import DATA_MAP, BY_KEY, DataKind, Paths, path_of

# 判定结果。只有 CLEANABLE 一种意味着「可以删」，另外三种都是「留着」。
PROTECTED = "protected_user_material"
CLEANABLE = "cleanable"
KEPT_UNKNOWN = "kept_unknown"
KEPT_TOO_YOUNG = "kept_too_young"
VERDICTS = (PROTECTED, CLEANABLE, KEPT_UNKNOWN, KEPT_TOO_YOUNG)

KEEP_DAYS = 30

_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Family:
    """一族可清理的东西：在哪个根下面，以及**什么形状**才算一个可清理的单位。

    形状这一层是要紧的。「在 logs 下面」会把 ``logs/external_agents``（可能夹带订单内容）一起圈进去；
    「logs 下面名字长得像日期的那一层目录」不会。
    """

    key: str
    label_zh: str
    selects: Callable[[tuple[str, ...]], bool]
    unit_zh: str

    @property
    def entry(self) -> DataKind:
        return BY_KEY[self.key]


def _direct_child(rest: tuple[str, ...]) -> bool:
    """根下面第一层的任何一项。"""
    return len(rest) == 1


def _date_dir(rest: tuple[str, ...]) -> bool:
    """根下面第一层、名字是 ``YYYY-MM-DD`` 的那种目录。"""
    return len(rest) == 1 and bool(_DATE_DIR.match(rest[0]))


# 恰好三族，和 owner 的裁决一一对应。多一族都加不进来而不被 tests/test_retention_rules.py 发现。
FAMILIES: tuple[Family, ...] = (
    Family("node_trials", "搭建台试跑记录", _direct_child, "一次试跑"),
    Family("runtime_logs", "系统运行日志", _date_dir, "一天的日志"),
    Family("skill_candidates", "技能候选（临时中转）", _direct_child, "一份候选"),
)


def _parts(path: Path | str) -> tuple[str, ...]:
    try:
        return tuple(os.path.normcase(part) for part in Path(path).parts)
    except (OSError, ValueError):
        return ()


def _under(path: Path | str, root: Path | str) -> tuple[str, ...] | None:
    """``path`` 相对 ``root`` 的剩余部分；不在它下面则返回 None。就是 ``root`` 本身时返回空元组。

    逐段按操作系统比较名字，大小写变体混不进来。**不解析链接**：解析了的话，试跑目录里一个指向工作区的
    目录联接会让工作区「看起来」落在可清理的根下面。
    """
    here, there = _parts(path), _parts(root)
    if not there or len(here) < len(there) or here[:len(there)] != there:
        return None
    return tuple(Path(path).parts[len(there):])


def protected_roots(paths: Paths, project_id: str = "") -> tuple[tuple[str, Path], ...]:
    """永远不许自动清理的根，连同它们的 key。由 data_map 自动生成，不手抄。

    容器只有在它本身就是**用户材料**时才算——工作区根目录是（别的项目的工作区也在它下面），数据总目录不是。
    这条界限是被测试打出来的：把所有「永久」的容器都算成保护根，数据总目录就成了保护根，而三族正住在它下面，
    于是什么都清不了。保护要盖住用户的东西，不是盖住产品自己的东西。
    """
    return tuple((entry.key, path_of(entry, paths, project_id)) for entry in DATA_MAP
                 if entry.retention == "forever" and (not entry.container or entry.kind == "user_material"))


def family_root(family: Family, paths: Paths, project_id: str = "") -> Path:
    return path_of(family.entry, paths, project_id)


@dataclass(frozen=True)
class Judgement:
    """一条路径的判定：结果、依据的那一类、以及说给人听的一句话。"""

    verdict: str
    key: str = ""
    why_zh: str = ""


def classify(path: Path | str, paths: Paths, *, project_id: str = "",
             modified: datetime | None = None, now: datetime | None = None,
             keep_days: int = KEEP_DAYS) -> Judgement:
    """这条路径能不能自动清理。四道闸，默认全拒。

    ``modified`` 是这条路径的修改时间，由调用方读好传进来（这个模块不碰磁盘）。没给就当它太新，保留。
    """
    # 第一道：保护。落在任何一个保护根之下，立刻定案，后面的规则没有机会推翻它。
    for key, root in protected_roots(paths, project_id):
        if _under(path, root) is not None:
            return Judgement(PROTECTED, key, f"{BY_KEY[key].label_zh}：永久保留，只有你自己能删")

    # 第二道：三族，并且形状要对。
    for family in FAMILIES:
        root = family_root(family, paths, project_id)
        rest = _under(path, root)
        if rest is None or not rest or not family.selects(tuple(os.path.normcase(p) for p in rest)):
            continue

        # 第三道：自我断言。防的是自己人——将来谁把规则改歪，炸掉的是清理器，不是用户的文件。
        if _under(path, root) is None:
            raise AssertionError(f"清理规则自相矛盾：{path} 被判为 {family.key}，却不在 {root} 之下")

        # 第四道：年龄。
        if modified is None:
            return Judgement(KEPT_TOO_YOUNG, family.key, "读不到修改时间，按太新处理")
        moment = now or datetime.now()
        if moment - modified < timedelta(days=max(0, int(keep_days))):
            return Judgement(KEPT_TOO_YOUNG, family.key,
                             f"{family.label_zh}：还不满 {keep_days} 天")
        return Judgement(CLEANABLE, family.key, f"{family.label_zh}：{family.unit_zh}，超过 {keep_days} 天")

    # 认不出来的一律保留。不知道的东西永远归用户。
    return Judgement(KEPT_UNKNOWN, "", "产品不认识这个位置，一律保留")


def assert_families_cannot_select_protected() -> None:
    """三族的选取规则不可能选中任何一个保护根。导入时跑一次：写歪了，程序起不来。

    用的是声明本身，不是某一台机器上的路径——这样它证明的是规则，而不是今天这台机器恰好没事。
    """
    home = Path("/__h__")
    paths = Paths(home=home, workplace_root=Path("/__w__"),
                  project_workplace=Path("/__w__/__p__"), project_dir=home / "projects" / "__p__")
    protected = protected_roots(paths, "__p__")
    for family in FAMILIES:
        root = family_root(family, paths, "__p__")
        for key, guarded in protected:
            rest = _under(guarded, root)
            if rest and family.selects(tuple(os.path.normcase(p) for p in rest)):
                raise AssertionError(
                    f"保留规则写歪了：{key}（{BY_KEY[key].label_zh}，永久保留）会被 {family.key} 当成可清理的"
                    f"{family.unit_zh}。三族的选取规则必须选不中任何一个保护根。")
        if family.entry.retention != "days_30":
            raise AssertionError(f"{family.key} 在数据清单里不是 30 天保留，却被列为可清理族")


assert_families_cannot_select_protected()
