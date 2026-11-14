"""注册/撤销一个 Windows 计划任务，并**把它指向哪里读回来核对一遍**。

抽出来是因为 2026-09-24 的那次事故：这台机器上每晚有一个备份任务成功退出、返回 0，而它备的是另一个仓库
的现场。根因有两层，这个模块把两层都堵上：

* 任务的命令行里**没写家目录**，脚本就退回「我在哪个仓库，就备哪个仓库的 `.ampuly`」。所以调用方必须把
  关键路径写进参数，这个模块负责在注册完之后**读回来确认那段路径真的在里面**。
* 检查的人只看「有没有叫这个名字的任务」。名字对、指向错，是最难发现的一种错——它每天报成功。

所以 :func:`install` 的成功条件不是「注册命令返回 0」，而是「读回来的参数里确实有我要它保护的那个路径」。

**为什么走 PowerShell 的 `Register-ScheduledTask` 而不是 `schtasks /TR`：** `/TR` 的值有 261 字符上限，
而「把家目录和备份目录都显式写进去」很容易超过它。超了之后最省事的做法是退回隐含默认——那恰好是这次
事故的根因。正式接口把可执行文件和参数分开存，没有这个坎，顺带也让读回来核对时只需要看参数那一项。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


class NotWindows(RuntimeError):
    """计划任务这套是 Windows 的；别的系统上让调用方自己说该怎么办。"""


def _powershell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, check=False,
    )


def _quoted(value: str) -> str:
    """单引号包起来，内部的单引号翻倍——PowerShell 字面量的转义方式。"""
    return "'" + str(value).replace("'", "''") + "'"


def install(*, name: str, execute: str, arguments: str, schedule: str, at: str,
            expect_in_arguments: str, day: str = "") -> tuple[int, list[str]]:
    """注册一个任务，返回 ``(退出码, 要打印给人看的几行话)``。

    ``expect_in_arguments`` 是注册完要在任务参数里找回来的那段文字——通常是它该保护的那个绝对路径。
    找不到就判失败：宁可让人当场看见「没对上」，也不要留下一个每天报成功却指向别处的任务。
    """
    if os.name != "nt":
        raise NotWindows("计划任务只在 Windows 上注册")
    kind = str(schedule).upper()
    if kind == "DAILY":
        trigger = f"New-ScheduledTaskTrigger -Daily -At {_quoted(at)}"
    elif kind == "WEEKLY":
        trigger = (f"New-ScheduledTaskTrigger -Weekly -DaysOfWeek {day or 'Sunday'} -At {_quoted(at)}")
    else:
        return 2, [f"不认识的排期：{schedule}"]
    script = (
        f"$a = New-ScheduledTaskAction -Execute {_quoted(execute)} -Argument {_quoted(arguments)}; "
        f"$t = {trigger}; "
        f"$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
        f"-DontStopIfGoingOnBatteries; "
        f"Register-ScheduledTask -TaskName {_quoted(name)} -Action $a -Trigger $t -Settings $s -Force "
        f"| Out-Null; 'registered'"
    )
    made = _powershell(script)
    said = [(made.stdout or "").strip() or (made.stderr or "").strip() or f"exit={made.returncode}"]
    if made.returncode != 0:
        return made.returncode, said
    back = _powershell(
        f"(Get-ScheduledTask -TaskName {_quoted(name)}).Actions "
        f"| ForEach-Object {{ $_.Execute + ' ' + $_.Arguments }}"
    )
    printed = (back.stdout or "")
    if expect_in_arguments and expect_in_arguments not in printed:
        said.append(f"！注册回来核对时，没在任务参数里看到：{expect_in_arguments}")
        said.append(f"  请手动检查： Get-ScheduledTask -TaskName {name} | %{{ $_.Actions }}")
        return 1, said
    said.append(f"已注册：{name}（{kind} {at}{(' ' + day) if day else ''}）")
    if expect_in_arguments:
        said.append(f"  已读回核对：任务参数里确实写着 {expect_in_arguments}")
    return 0, said


def uninstall(*, name: str) -> tuple[int, list[str]]:
    """撤销注册。「能装上」不等于「能拿掉」——装之前就该知道怎么退。"""
    if os.name != "nt":
        raise NotWindows("计划任务只在 Windows 上注册")
    gone = _powershell(
        f"Unregister-ScheduledTask -TaskName {_quoted(name)} -Confirm:$false; 'removed'")
    return gone.returncode, [(gone.stdout or "").strip() or (gone.stderr or "").strip()
                             or f"exit={gone.returncode}"]


# ── 产品自己的那几个计划任务：看得见，也拿得掉 ────────────────────────────────

# 只认这两个脚本。别人的计划任务一律不列、更不删——这个模块碰的必须只有产品自己装的东西。
OUR_SCRIPTS = ("backup_runtime.py", "product_backup.py")
_HOME = re.compile(r'--home\s+"([^"]+)"|--home\s+(\S+)')


def _protects(arguments: str) -> tuple[str, bool]:
    """这个任务保护的是哪个家目录，以及那是**写明的**还是**推出来的**。

    写明的就照读。没写明时，脚本会退回「我在哪个仓库，就备哪个仓库的 `.ampuly`」——2026-09-24 那次事故就是
    这么来的：一个任务名字对、指向的却是另一个仓库，每晚报成功。所以这里把推出来的那个也算出来，并**标明
    它是推的**：看得见「这个任务没说它备哪儿」，才有机会发现它备错了。
    """
    found = _HOME.search(arguments or "")
    if found:
        return (found.group(1) or found.group(2) or ""), False
    for script in OUR_SCRIPTS:
        at = (arguments or "").find(script)
        if at < 0:
            continue
        # head 是脚本所在的**目录**，不是脚本本身——拼回完整路径再上溯，否则会少数一层。
        head = (arguments or "")[:at].strip().strip('"')
        try:
            return str((Path(head) / script).parent.parent / ".ampuly"), True
        except (OSError, ValueError):
            return "", True
    return "", True


def product_tasks(this_home: str | Path = "") -> list[dict[str, Any]]:
    """这台机器上，产品自己装过的备份计划任务。只读。

    连**别的安装**装的也一起列出来——那正是要看见的东西：一个指向另一个仓库的任务，光看名字看不出问题。
    """
    if os.name != "nt":
        return []
    script = (
        "Get-ScheduledTask | Where-Object { $_.Actions.Arguments -match 'backup_runtime|product_backup' } | "
        "ForEach-Object { $i = Get-ScheduledTaskInfo -TaskName $_.TaskName; "
        "[pscustomobject]@{ name = $_.TaskName; arguments = ($_.Actions.Arguments -join ' '); "
        "execute = ($_.Actions.Execute -join ' '); state = [string]$_.State; "
        "last_result = $i.LastTaskResult; last_run = [string]$i.LastRunTime; "
        "next_run = [string]$i.NextRunTime } } | ConvertTo-Json -Compress"
        # 不用 -AsArray：那是 PowerShell 6+ 才有的参数，这台机器上的 Windows PowerShell 5.1 会
        # 静默吐出空结果——「查不到」和「一个都没有」长得一模一样。只有一个任务时它给的是单个
        # 对象而不是数组，下面按这一点解包。
    )
    said = _powershell(script).stdout.strip()
    if not said:
        return []
    try:
        rows = json.loads(said)
    except ValueError:
        return []
    mine = ""
    try:
        mine = str(Path(this_home).resolve()) if this_home else ""
    except OSError:
        mine = str(this_home or "")
    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else [rows]:
        arguments = str(row.get("arguments", "") or "")
        protects, implicit = _protects(arguments)
        try:
            same = bool(mine) and bool(protects) and str(Path(protects).resolve()) == mine
        except OSError:
            same = False
        out.append({
            "name": str(row.get("name", "") or ""),
            "execute": str(row.get("execute", "") or ""),
            "arguments": arguments,
            "state": str(row.get("state", "") or ""),
            "last_result": int(row.get("last_result", 0) or 0),
            "last_run": str(row.get("last_run", "") or ""),
            "next_run": str(row.get("next_run", "") or ""),
            "protects": protects,
            "protects_implicitly": implicit,
            "this_install": same,
        })
    return sorted(out, key=lambda item: item["name"])


def forget(*, name: str, this_home: str | Path = "") -> tuple[int, list[str]]:
    """拿掉一个计划任务——**只拿得掉产品自己装的那几个**。

    先问一遍 :func:`product_tasks`：名字不在里面就拒绝。控制台上的一个按钮不该能删掉这台机器上任意一个
    计划任务；它只该能删掉产品自己造出来的那些。
    """
    known = {task["name"] for task in product_tasks(this_home)}
    if name not in known:
        return 1, [f"「{name}」不是产品装的备份任务，不动它。"]
    return uninstall(name=name)
