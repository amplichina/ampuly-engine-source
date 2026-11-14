"""自动备份上次跑得怎么样，以及**它备的是哪个家目录**。

2026-09-24 查出一件事，这个模块整个是为它而写的：这台机器上每晚都有一个备份任务成功退出、返回 0，而它
备的是**另一个仓库**的现场。安普里这边自 09-04 起一次自动备份都没有过，任务每天报成功。

从中学到的问法变了：

* **别问「有没有注册计划任务」。** 注册着的那个恰恰是骗过所有人的那个——名字对、指向错。
* **要问「上次真的备了吗、备的是哪一个家目录」。** 这两个问题的答案就写在备份脚本每次跑完留下的回执里，
  而回执是跑出来的，不是声明出来的。

所以这里只读回执，一个字都不问任务计划程序。没有回执，意思就是「从来没自动跑过」——那本身就是答案。

最要紧的一项是 :func:`_reading` 里的 ``protects_this_home``：回执里的家目录和这台控制台自己的家目录对不
上时，界面要把它当成警告来显示。那正是当初没人看见的那件事。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

AUTO_DIR = ("backups", "auto")
DAILY_RECEIPT = "last-run.json"
WEEKLY_RECEIPT = "last-archive.json"


def _reading(home: Path, name: str) -> dict[str, Any]:
    """读一张回执。读不到就如实说「从来没跑过」，不猜。"""
    path = Path(home).joinpath(*AUTO_DIR, name)
    if not path.is_file():
        return {"ran": False, "why_zh": "还没自动备份过"}
    try:
        said = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ran": False, "why_zh": "上次的记录读不出来了"}
    protected = str(said.get("home", "") or "")
    try:
        mine = str(Path(home).resolve())
    except OSError:
        mine = str(home)
    return {
        "ran": True,
        "ok": bool(said.get("ok")),
        "at": str(said.get("at", "") or ""),
        "home": protected,
        "note": str(said.get("note", "") or ""),
        # 这一项是整个模块存在的理由：备的是不是**这一套**数据。
        "protects_this_home": bool(protected) and protected == mine,
        "pruned": [str(item) for item in (said.get("pruned") or [])],
    }


def auto_backup_health(home: Path) -> dict[str, Any]:
    """两档自动备份各自上次跑得怎么样。只读。

    日备管四个数据库（业务真相），周备管整机归档（技能、提示词、组织配置这些**不可再生**的东西）。两档
    分开说：只报「有备份」会把「数据库备了、资产没备」这种情况藏起来。
    """
    mine = ""
    try:
        mine = str(Path(home).resolve())
    except OSError:
        mine = str(home)
    daily = _reading(Path(home), DAILY_RECEIPT)
    weekly = _reading(Path(home), WEEKLY_RECEIPT)
    wrong = [label for label, said in (("每天的备份", daily), ("每周的备份", weekly))
             if said.get("ran") and not said.get("protects_this_home")]
    warnings: list[dict[str, str]] = []
    if wrong:
        warnings.append({
            "code": "backs_up_another_home",
            "why_zh": "、".join(wrong) + "上次备的不是这里的数据。看着像有备份，其实没有——"
                      "名字对得上，备的地方不对，而且每次都报成功。",
        })
    for label, said in (("每天的备份", daily), ("每周的备份", weekly)):
        if said.get("ran") and not said.get("ok"):
            warnings.append({"code": "last_run_failed", "why_zh": f"{label}上次没做成：{said.get('note', '')}"})
    return {"this_home": mine, "daily": daily, "weekly": weekly, "warnings": warnings}
