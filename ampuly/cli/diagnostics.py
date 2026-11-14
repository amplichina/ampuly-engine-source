"""Read-only production diagnostics for one SuperOnes runtime home.

Diagnostics report facts and conservative candidates.  They never repair,
cancel, retry, vacuum, delete or release anything; those operations belong to
their existing governed paths.
"""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DATABASES = ("business.db", "global.db", "ui_state.db", "channel_inbound.db")
_ACTIVE_TASK_STATES = ("in_progress", "running", "executing", "stopping", "resuming")


@dataclass(frozen=True)
class Finding:
    key: str
    severity: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiagnosticReport:
    home: str
    checked_at: str
    port: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        counts = {level: sum(item.severity == level for item in self.findings) for level in ("ok", "warning", "error")}
        return {
            "schema": "superones-diagnostic/1",
            "home": self.home,
            "checked_at": self.checked_at,
            "port": self.port,
            "ok": self.ok,
            "counts": counts,
            "findings": [asdict(item) for item in self.findings],
            "mutations_performed": 0,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def lines(self) -> list[str]:
        return [f"[{item.severity}] {item.key}: {item.summary}" for item in self.findings]


def _listening(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


def _connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=1.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _scalar(connection: sqlite3.Connection, sql: str, parameters: tuple[Any, ...] = ()) -> int:
    row = connection.execute(sql, parameters).fetchone()
    return int(row[0] or 0) if row else 0


def _database_findings(home: Path) -> tuple[list[Finding], dict[str, sqlite3.Connection]]:
    findings: list[Finding] = []
    opened: dict[str, sqlite3.Connection] = {}
    for name in DATABASES:
        path = home / name
        if not path.is_file():
            findings.append(Finding(f"database.{name}", "warning", "database is absent", {"bytes": 0}))
            continue
        try:
            connection = _connection(path)
            result = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if result != "ok":
                findings.append(Finding(f"database.{name}", "error", "quick_check failed", {"result": result}))
                connection.close()
                continue
            opened[name] = connection
            findings.append(Finding(f"database.{name}", "ok", "read-only quick_check passed", {"bytes": path.stat().st_size}))
        except sqlite3.Error as exc:
            findings.append(Finding(f"database.{name}", "error", "database could not be read", {"error_type": type(exc).__name__}))
    return findings, opened


def _runtime_findings(connections: dict[str, sqlite3.Connection], *, console_up: bool, stale_minutes: int) -> list[Finding]:
    findings: list[Finding] = []
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max(1, stale_minutes))).isoformat()
    business = connections.get("business.db")
    if business is not None:
        unknown = _scalar(business, "SELECT COUNT(*) FROM budget_reservations WHERE state = 'unknown'") if _table(business, "budget_reservations") else 0
        level = "warning" if unknown else "ok"
        summary = f"{unknown} reservation(s) require independent verification" if unknown else "no unknown budget reservation"
        findings.append(Finding("budget.unknown", level, summary, {"count": unknown}))
        open_budgets = _scalar(business, "SELECT COUNT(*) FROM run_budgets WHERE state = 'open'") if _table(business, "run_budgets") else 0
        findings.append(Finding("budget.open", "ok", f"{open_budgets} open run budget(s)", {"count": open_budgets}))

    runtime = connections.get("global.db")
    if runtime is not None:
        claimed = _scalar(
            runtime, "SELECT COUNT(*) FROM effects WHERE state = 'claimed' AND claimed_at < ?", (cutoff,),
        ) if _table(runtime, "effects") else 0
        outcome_unknown = _scalar(runtime, "SELECT COUNT(*) FROM effects WHERE state = 'outcome_unknown'") if _table(runtime, "effects") else 0
        outbox_failed = _scalar(runtime, "SELECT COUNT(*) FROM effect_outbox WHERE status = 'failed'") if _table(runtime, "effect_outbox") else 0
        active_tasks = 0
        if _table(runtime, "tasks"):
            placeholders = ",".join("?" for _ in _ACTIVE_TASK_STATES)
            active_tasks = _scalar(
                runtime,
                f"SELECT COUNT(*) FROM tasks WHERE status IN ({placeholders}) AND created_at < ?",
                (*_ACTIVE_TASK_STATES, cutoff),
            )
        orphan_candidates = claimed + (active_tasks if not console_up else 0)
        level = "warning" if orphan_candidates else "ok"
        findings.append(Finding(
            "runtime.orphan_candidates", level,
            f"{orphan_candidates} stale in-flight record(s) need governed reconciliation" if orphan_candidates else "no stale in-flight candidate",
            {"claimed_effects": claimed, "active_tasks_while_console_down": active_tasks if not console_up else 0, "stale_minutes": stale_minutes},
        ))
        action_count = outcome_unknown + outbox_failed
        findings.append(Finding(
            "effects.attention", "warning" if action_count else "ok",
            f"{outcome_unknown} unknown outcome(s), {outbox_failed} failed outbox event(s)",
            {"outcome_unknown": outcome_unknown, "failed_outbox": outbox_failed},
        ))
    return findings


def diagnose(home: Path, *, port: int = 8765, stale_minutes: int = 30) -> DiagnosticReport:
    resolved = home.resolve()
    report = DiagnosticReport(
        home=str(resolved), checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), port=port,
    )
    if not resolved.is_dir():
        report.findings.append(Finding("home", "error", "runtime home does not exist"))
        return report

    console_up = _listening(port)
    lock_exists = (resolved / "office_ui.lock").is_file()
    report.findings.append(Finding(
        "console", "ok", "console is listening" if console_up else "console is not listening",
        {"listening": console_up, "lock_file_present": lock_exists},
    ))
    if console_up and not lock_exists:
        report.findings.append(Finding(
            "single_instance", "warning", "something answers on the console port but this home has no instance-lock file",
        ))
    else:
        report.findings.append(Finding(
            "single_instance", "ok", "port and instance-lock presence are not contradictory",
            {"lock_file_present": lock_exists},
        ))

    usage = shutil.disk_usage(resolved)
    if usage.free < 512 * 1024 * 1024:
        severity, summary = "error", "less than 512 MiB free; runs and SQLite commits are at immediate risk"
    elif usage.free < 2 * 1024 * 1024 * 1024:
        severity, summary = "warning", "less than 2 GiB free; prepare capacity before large runs or upgrades"
    else:
        severity, summary = "ok", "free disk is above the 2 GiB operational floor"
    report.findings.append(Finding("disk", severity, summary, {"free_bytes": usage.free, "total_bytes": usage.total}))

    db_findings, connections = _database_findings(resolved)
    report.findings.extend(db_findings)
    try:
        report.findings.extend(_runtime_findings(connections, console_up=console_up, stale_minutes=stale_minutes))
    except sqlite3.Error as exc:
        report.findings.append(Finding("runtime_state", "error", "runtime state queries failed", {"error_type": type(exc).__name__}))
    finally:
        for connection in connections.values():
            connection.close()

    log_root = resolved / "logs"
    latest = max((path.stat().st_mtime for path in log_root.glob("*/*") if path.is_file()), default=0.0) if log_root.is_dir() else 0.0
    report.findings.append(Finding(
        "logs", "ok" if latest else "warning", "log files are present" if latest else "no log file was found",
        {"latest_mtime": latest},
    ))
    return report
