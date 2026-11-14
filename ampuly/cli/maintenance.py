"""``ampuly maintenance``: the stop-the-engine chores, one command, with a report.

* VACUUM the runtime databases (``global.db`` keeps every freed page after an
  order is deleted: 428 MB of which 19 MB were live on 2026-09-02);
* prune log days older than the retention;
* report sizes before and after.

It refuses to touch the databases while a console is running against this
AMPULY home -- it holds ``office_ui.lock`` for its whole life -- or anything
answers on a console port (8765 upstream, 8766 Ampuly), because VACUUM needs
to be the only writer. Log days are kept 30 days by default (owner ruling,
2026-09-22: system logs 30 days).
"""

from __future__ import annotations

import shutil
import socket
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from ampuly.core.file_lock import lock_file_descriptor, unlock_file_descriptor

# The ports a console answers on: upstream `ampuly ui` (8765) and the Ampuly launcher (8766).
CONSOLE_PORTS: tuple[int, ...] = (8765, 8766)
KEEP_LOG_DAYS = 30


@dataclass
class MaintenanceReport:
    databases: list[dict[str, object]] = field(default_factory=list)
    logs_removed: list[str] = field(default_factory=list)
    browser_profiles_removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out: list[str] = []
        for db in self.databases:
            if db.get("planned"):
                out.append(f"{db['path']}: {db['before_mb']:.1f} MB (VACUUM planned; add --apply)")
                continue
            out.append(
                f"{db['path']}: {db['before_mb']:.1f} MB -> {db['after_mb']:.1f} MB"
                + (f" ({db['integrity']})" if db.get("integrity") else "")
            )
        if self.logs_removed:
            out.append("logs removed: " + ", ".join(self.logs_removed))
        for item in self.skipped:
            out.append("skipped: " + item)
        return out


def console_holds_home(ampuly_home: Path) -> bool:
    """Whether a console is running against this AMPULY home, whatever its port: the office UI holds
    ``office_ui.lock`` from start to exit (plugins/office_ui/server.py). A lock that cannot be checked
    counts as held -- VACUUM under a live writer is the one thing this command must never do."""
    lock_path = Path(ampuly_home) / "office_ui.lock"
    if not lock_path.is_file():
        return False
    try:
        handle = open(lock_path, "a+", encoding="utf-8")
    except OSError:
        return True
    try:
        try:
            lock_file_descriptor(handle.fileno(), blocking=False)
        except OSError:
            return True
        try:
            unlock_file_descriptor(handle.fileno())
        except OSError:
            pass
        return False
    finally:
        handle.close()


def console_listening(port: int = 8765, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def runtime_databases(ampuly_home) -> list:
    """这次要回收空间的库：总目录根下那两个，**加上每个项目自己的任务库**。

    只写死那两个名字的时候，项目的任务库一个都不在名单里——而删单删的正是它们里面的行。模块开头那句
    「428 MB 里只有 19 MB 是活的」说的是 global.db，同一件事在每个项目的库里都在发生，只是没人来收。
    """
    found = [ampuly_home / name for name in ("global.db", "ui_state.db")]
    projects = ampuly_home / "projects"
    if projects.is_dir():
        found.extend(sorted(child / "tasks.db" for child in projects.iterdir()))
    return [path for path in found if path.is_file()]


def vacuum_database(path: Path) -> dict[str, object]:
    before = path.stat().st_size / 1_048_576 if path.is_file() else 0.0
    conn = sqlite3.connect(str(path))
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        conn.execute("VACUUM")
    finally:
        conn.close()
    after = path.stat().st_size / 1_048_576 if path.is_file() else 0.0
    return {"path": str(path), "before_mb": before, "after_mb": after, "integrity": integrity}


def prune_logs(log_dir: Path, keep_days: int, *, apply: bool = True) -> list[str]:
    """够年头的日志日目录。``apply=False`` 只列出来，一个都不删（数据与文件 P3）。

    只认 ``YYYY-MM-DD`` 这样的日目录，所以正在被写的那份运行日志（它躺在 logs 根下）和
    ``logs/external_agents``（可能夹带订单内容）都不在其中——判定与 :mod:`ampuly.core.retention` 的第二道闸一致。
    """
    removed: list[str] = []
    if not log_dir.is_dir() or keep_days <= 0:
        return removed
    cutoff = date.today() - timedelta(days=keep_days)
    for child in sorted(log_dir.iterdir()):
        if not child.is_dir():
            continue
        try:
            day = datetime.strptime(child.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            if apply:
                shutil.rmtree(child, ignore_errors=True)
            removed.append(child.name)
    return removed


def run_maintenance(ampuly_home: Path, *, keep_log_days: int = KEEP_LOG_DAYS, port: int | None = None, force: bool = False,
                    keep_browser_profile_days: int = 30, apply: bool = False) -> MaintenanceReport:
    """压缩运行库、清理够年头的日志。**默认只列不删**（数据与文件 P3）。

    owner 的裁决：清理要先把将要删除的内容列出来，确认之后再删。所以 ``apply`` 默认为假——这个函数算出会删
    什么、如实报出来，一个文件都不动。命令行加 ``--apply`` 才真删。

    ``keep_browser_profile_days`` 保留在签名里，但**不再有人用它**：浏览器档案可能装着客户的登录态，而它不在
    owner 点名的三族里，所以产品不再自动删它。登录怎么保存是 B8 的事；在那之前，多留着不是损失。
    """
    report = MaintenanceReport()
    ports = tuple(dict.fromkeys([*( [port] if port else []), *CONSOLE_PORTS]))
    busy_port = next((p for p in ports if console_listening(p)), None)
    if console_holds_home(ampuly_home):
        # Not even --force: a held lock is a console writing these databases right now.
        report.skipped.append(f"a console is running against {ampuly_home} (office_ui.lock is held); stop it first")
    elif busy_port is not None and not force:
        report.skipped.append(f"something is listening on console port {busy_port}; stop `ampuly ui` first (or pass --force if you know no engine is running)")
    else:
        for path in runtime_databases(ampuly_home):
            if apply:
                report.databases.append(vacuum_database(path))
            else:
                size_mb = path.stat().st_size / 1_048_576
                report.databases.append({
                    "path": str(path), "before_mb": size_mb, "after_mb": size_mb, "planned": True,
                })
        # 浏览器档案曾经在这里按 30 天清掉。不再清：它可能装着客户的登录态，而 owner 点名可自动清理的只有
        # 三族——试跑记录、运行日志、临时中转文件，档案不在其中。规则说该留的，这里就不能删。
        report.browser_profiles_removed = []
        # 日志的清理原本写在这个 else 外面：控制台正在跑、别的都跳过时，它照样在删。现在跟着一起跳过。
        report.logs_removed = prune_logs(ampuly_home / "logs", keep_log_days, apply=apply)
        if not apply and report.logs_removed:
            report.skipped.append(
                f"只列不删：上面这 {len(report.logs_removed)} 天的日志会被清理，加 --apply 才真删")
    return report
