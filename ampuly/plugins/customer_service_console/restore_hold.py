"""A restored customer-service tree is history, not a send grant.

Backup copies the acceptance, outbox and channel-binding files so an owner can
read what happened. Restore then pauses live bindings and holds unfinished
outbound commands. Sending needs a new authorization; the fence file is how
the channel path refuses until that happens.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

FENCE = "send-authority.json"
_HELD_REASON = "restored_needs_reauthorization"


def fence_path(storage: Path) -> Path:
    return Path(storage) / FENCE


def send_blocked(storage: Path) -> bool:
    path = fence_path(storage)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(data, dict):
        return True
    return str(data.get("send_authority") or "") != "granted"


def write_send_hold(storage: Path) -> None:
    root = Path(storage)
    root.mkdir(parents=True, exist_ok=True)
    fence_path(root).write_text(
        json.dumps(
            {"send_authority": "requires_recheck", "reason": "backup_restore"},
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def clear_send_hold(storage: Path) -> None:
    """A new selection after pause is the re-authorization. Drop the fence."""
    path = fence_path(storage)
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        return


def hold_restored_sends(storage: Path) -> dict[str, int]:
    """Pause enabled bindings and cancel drafts that have not left the machine.

    History rows stay. ``dispatching`` / ``unknown`` keep their status: those
    may already have had an external effect, so restore does not replay them
    and does not pretend they never happened. The send fence blocks a new send
    until someone authorizes again.
    """
    root = Path(storage)
    summary = {"bindings_paused": 0, "commands_held": 0}
    if not root.is_dir():
        return summary
    binding_file = root / "channel-bindings.sqlite"
    if binding_file.is_file():
        summary["bindings_paused"] = _pause_bindings(binding_file)
    for path in sorted(root.glob("*.sqlite")):
        summary["commands_held"] += _hold_draft_commands(path)
    write_send_hold(root)
    return summary


def _pause_bindings(path: Path) -> int:
    from ampuly.plugins.customer_service_console.bindings import BindingStore

    enabled: list[str] = []
    connection = sqlite3.connect(str(path))
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "bindings" not in tables:
            return 0
        latest: dict[str, dict] = {}
        for (raw,) in connection.execute("SELECT record_json FROM bindings ORDER BY sequence"):
            record = json.loads(raw)
            if not isinstance(record, dict):
                continue
            binding = record.get("binding") if isinstance(record.get("binding"), dict) else record
            if isinstance(binding.get("account_id"), str):
                latest[binding["account_id"]] = binding
        enabled = [account for account, record in latest.items() if record.get("enabled") is True]
    finally:
        connection.close()
    if not enabled:
        return 0
    store = BindingStore(path, authorize=lambda: None)
    paused = 0
    for account_id in enabled:
        store.pause(account_id, actor="restore")
        paused += 1
    return paused


def _hold_draft_commands(path: Path) -> int:
    connection = sqlite3.connect(str(path))
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "commands" not in tables:
            return 0
        columns = {row[1] for row in connection.execute("PRAGMA table_info(commands)")}
        if not {"status", "reason", "id"} <= columns:
            return 0
        now = time.time()
        if "updated_at" in columns:
            cursor = connection.execute(
                "UPDATE commands SET status = 'cancelled', reason = ?, updated_at = ? "
                "WHERE status IN ('draft', 'approved')",
                (_HELD_REASON, now),
            )
        else:
            cursor = connection.execute(
                "UPDATE commands SET status = 'cancelled', reason = ? WHERE status IN ('draft', 'approved')",
                (_HELD_REASON,),
            )
        connection.commit()
        return int(cursor.rowcount or 0)
    except sqlite3.Error:
        return 0
    finally:
        connection.close()
