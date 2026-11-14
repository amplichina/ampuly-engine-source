"""数据库里有多少空页——删掉的东西腾出来、却还没还给磁盘的那部分。

SQLite 删行不还空间：页被标记成空闲留在文件里，下次写入复用。只增不减的后果在这个仓库里真实发生过，
`ampuly/cli/maintenance.py` 的模块头记着：**428 MB 的库里只有 19 MB 是活的**（2026-09-02）。

回收空间要执行 `VACUUM`，而 `VACUUM` 必须是唯一的写者——所以它在控制台运行时整段跳过，而控制台基本
一直开着。也就是说这件事**实际上从来没跑过**，而且没有任何东西会在它变严重时出声。

这个模块只负责出声：它**只读**，一个字节都不动。2026-09-24 实测这台机器上空页合计 0.00 MB，所以现在
还不用回收——但「现在是零」和「有人盯着」是两回事，后者才是当初缺的那个。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

# 什么时候才值得停一次控制台去回收。两个条件都要满足：
#   * 绝对量够大——为了几 MB 停服务不划算；
#   * 占比够高——一个 2 GB 的库里 100 MB 空页不算病，一个 200 MB 的库里 100 MB 是。
WORTH_RECLAIMING_MB = 50.0
WORTH_RECLAIMING_SHARE = 0.25


def _one(path: Path) -> dict[str, Any] | None:
    """量一个库。读不出来就返回 ``None``——量不了不等于没问题，但也不该瞎报一个数。"""
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        page = int(conn.execute("PRAGMA page_size").fetchone()[0])
        pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
        free = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    try:
        size = path.stat().st_size
    except OSError:
        size = pages * page
    return {
        "name": path.name,
        "path": str(path),
        "bytes": size,
        "dead_bytes": free * page,
        "dead_share": (free / pages) if pages else 0.0,
    }


def runtime_databases(home: Path) -> list[Path]:
    """产品自己的那几个库：总目录根下的四个，加上每个项目自己的任务库。"""
    home = Path(home)
    found = [home / name for name in ("business.db", "global.db", "ui_state.db", "channel_inbound.db")]
    projects = home / "projects"
    if projects.is_dir():
        try:
            found.extend(sorted(child / "tasks.db" for child in projects.iterdir() if child.is_dir()))
        except OSError:
            pass
    return [path for path in found if path.is_file()]


def dead_space(home: Path) -> dict[str, Any]:
    """每个库里有多少空页，以及**现在值不值得为它停一次控制台**。只读。"""
    rows = [row for row in (_one(path) for path in runtime_databases(home)) if row is not None]
    dead = sum(int(row["dead_bytes"]) for row in rows)
    total = sum(int(row["bytes"]) for row in rows)
    share = (dead / total) if total else 0.0
    worth = dead >= WORTH_RECLAIMING_MB * 1_048_576 and share >= WORTH_RECLAIMING_SHARE
    if not rows:
        why = "没有读到任何数据库"
    elif not dead:
        why = "没有可以腾出来的空间"
    elif worth:
        why = (f"有 {dead / 1_048_576:.0f} MB 是删过东西之后没还给硬盘的空间（占 {share:.0%}）。"
               "要把它腾出来，得先把控制台停一下。")
    else:
        why = (f"有 {dead / 1_048_576:.1f} MB 空着没还给硬盘（占 {share:.0%}），还不值得为它停一次控制台；"
               "留着，下次存东西会自己用上。")
    return {
        "databases": rows,
        "bytes": total,
        "dead_bytes": dead,
        "dead_share": share,
        "worth_reclaiming": worth,
        "why_zh": why,
    }
