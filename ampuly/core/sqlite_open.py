"""打开一个 SQLite 连接，用完**关掉**。

写这个模块是因为同一个错在这个仓库里出现了三次，而且每次的症状都不长得像数据库问题：

`with sqlite3.connect(...)` 看起来像「用完就关」，其实不是。sqlite3 的上下文管理器**只负责提交事务，从不
关闭连接**。漏下的那个句柄在 Windows 上把文件锁住，于是紧接着的删除、改名、搬迁会失败——而这些动作多半
用 `ignore_errors=True`，失败是不出声的。最后看到的是「删除成功」，可文件还在。

2026-09-24 就是这么发现的：删一个项目时，它的运行库删不掉，因为删之前那次「有没有单在跑」的检查把库打开
了没关。仓库里其实早就写着这条教训（`BusinessStore._connect` 的注释），只是没人能拦住第四次。

所以有了这个模块，和一条结构性测试：产品代码里不许再出现 `with sqlite3.connect(`。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def opened(path: str | Path, *, read_only: bool = False, timeout: float = 5.0,
           row_factory: object | None = None) -> Iterator[sqlite3.Connection]:
    """一个用完就真的关掉的 SQLite 连接。

    ``read_only=True`` 以只读方式打开（`mode=ro`），适合「只是看一眼」的检查——它们正是最容易忘记关的
    那一类。正常退出时提交，出异常时回滚，两种情况都关闭。
    """
    target = str(path)
    uri = f"file:{target}?mode=ro" if read_only else target
    conn = sqlite3.connect(uri, timeout=timeout, uri=read_only)
    if row_factory is not None:
        conn.row_factory = row_factory  # type: ignore[assignment]
    try:
        yield conn
        if not read_only:
            conn.commit()
    except Exception:
        if not read_only:
            conn.rollback()
        raise
    finally:
        conn.close()
