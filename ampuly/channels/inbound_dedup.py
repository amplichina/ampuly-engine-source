"""Recognising a message we have already handled, including after a restart.

Providers redeliver. A webhook that does not get its acknowledgement in time
is sent again, and a long-lived socket reconnects and replays. Handling the
same customer message twice can mean two work orders, two runs and two bills
for one request, so each channel keeps a set of the message ids it has seen.

Two things were wrong with keeping that set in memory. It grew forever in at
least one channel, which a process running for weeks eventually notices. And
it was forgotten on restart -- precisely when a provider is most likely to
redeliver, because the restart is why the acknowledgement never arrived.

So the set is bounded, and it can be durable. Durability is best-effort by
design: if the file cannot be opened we keep working in memory and *say* the
store is unavailable, because a channel that stops accepting customer messages
because its bookkeeping is unhappy has turned a small problem into an outage.

One thing this deliberately does not do: treat a message with no id as new. It
is neither seen nor unseen -- there is nothing to compare -- and the caller is
told exactly that, so "we cannot tell" never gets rounded to "it is fine".
"""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from pathlib import Path
from typing import Any

CAPACITY = 2000
_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_seen (
    channel TEXT NOT NULL,
    message_id TEXT NOT NULL,
    at TEXT NOT NULL DEFAULT (datetime('now')),
    seq INTEGER PRIMARY KEY AUTOINCREMENT
);
CREATE UNIQUE INDEX IF NOT EXISTS inbound_seen_key ON inbound_seen(channel, message_id);
"""


class InboundDedup:
    """Have we handled this one already?

    ``remember`` returns True the first time an id is offered and False every
    time after, so a caller reads it as "this is mine to process".
    """

    def __init__(self, channel: str, *, path: Path | str | None = None, capacity: int = CAPACITY) -> None:
        self.channel = str(channel or "").strip() or "channel"
        self.capacity = max(1, int(capacity))
        self._memory: OrderedDict[str, None] = OrderedDict()
        self._path = Path(str(path)) if path else None
        self.durable = False
        self.unavailable_reason = ""
        if self._path is not None:
            self._open()

    def _open(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
            self.durable = True
        except (OSError, sqlite3.Error) as exc:
            # Working memory-only is a degradation, not a failure; refusing
            # customer messages because a bookkeeping file is unhappy would be
            # the larger outage.
            self.durable = False
            self.unavailable_reason = f"{type(exc).__name__}: {exc}"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def remember(self, message_id: Any) -> bool:
        """True when this id is new to us and the caller should proceed."""
        clean = str(message_id or "").strip()
        if not clean:
            # Nothing to compare. Not "new" -- unknown. Callers that want to
            # act on that distinction should ask ``can_check`` first.
            return True
        if clean in self._memory:
            self._memory.move_to_end(clean)
            return False
        if self.durable and self._seen_durably(clean):
            self._note(clean)
            return False
        self._note(clean)
        self._store(clean)
        return True

    @staticmethod
    def can_check(message_id: Any) -> bool:
        """Whether this message can be recognised again at all."""
        return bool(str(message_id or "").strip())

    def _note(self, message_id: str) -> None:
        self._memory[message_id] = None
        while len(self._memory) > self.capacity:
            self._memory.popitem(last=False)

    def _seen_durably(self, message_id: str) -> bool:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM inbound_seen WHERE channel = ? AND message_id = ?",
                    (self.channel, message_id),
                ).fetchone()
            return row is not None
        except sqlite3.Error:
            # An unreadable store cannot say "already handled", and inventing
            # that answer would drop a real customer message.
            return False

    def _store(self, message_id: str) -> None:
        if not self.durable:
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO inbound_seen(channel, message_id) VALUES(?, ?)",
                    (self.channel, message_id),
                )
                # Keep only this channel's most recent ids: a provider does not
                # redeliver last year's message, and an unbounded table is the
                # problem this class exists to avoid.
                conn.execute(
                    "DELETE FROM inbound_seen WHERE channel = ? AND seq <= ("
                    "  SELECT seq FROM inbound_seen WHERE channel = ? ORDER BY seq DESC LIMIT 1 OFFSET ?"
                    ")",
                    (self.channel, self.channel, self.capacity),
                )
        except sqlite3.Error as exc:
            self.durable = False
            self.unavailable_reason = f"{type(exc).__name__}: {exc}"

    def describe(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "durable": self.durable,
            "capacity": self.capacity,
            "in_memory": len(self._memory),
            "unavailable_reason": self.unavailable_reason,
        }


def default_path(ampuly_home: Path | str | None = None) -> Path:
    """Where the durable set lives: its own file, next to the other stores.

    Its own file on purpose -- this is channel bookkeeping, not business truth
    or runtime state, and it must never be a reason to migrate either of them.
    """
    if ampuly_home is None:
        from ampuly.core.config import get_ampuly_home

        ampuly_home = get_ampuly_home()
    return Path(str(ampuly_home)) / "channel_inbound.db"
