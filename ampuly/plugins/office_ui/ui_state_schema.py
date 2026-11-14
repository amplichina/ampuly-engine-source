"""One shape for the ``server_state`` table in ``ui_state.db``.

The database has one owner -- the office-UI plugin -- and two entry points.
The server opens it through ``AgentStore`` (aiosqlite); the interactive CLI
opens it directly with stdlib ``sqlite3`` so a session started without the
server still remembers its mode, profile and preferred agent. Either can be
the first to run on a fresh machine, so both create the table -- but from
here, not from a literal of their own. Two copies of a DDL is how two entry
points drift apart while every test still passes.
"""

from __future__ import annotations

SERVER_STATE_DDL = (
    "CREATE TABLE IF NOT EXISTS server_state ("
    "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
)
