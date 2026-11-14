"""Where a Provider's session lives in our runtime facts, and what it did.

A Provider gives back an id of its own -- a Codex thread, a Claude session --
and everything after that depends on being able to say which of our runs it
belongs to. Without a durable mapping, a console restart turns a live session
into an orphan: nobody can resume it, nobody can attribute what it spent, and
the honest thing left to say is "lost".

So the mapping is written down beside the runtime facts it belongs to, not in
the business database: an order must never learn what a Codex thread id is.
Each session also carries a gap-free event sequence, which is what lets a
reconnect ask for "everything after 41" instead of replaying a turn or
silently missing the middle of one.

Nothing here starts, stops or talks to a Provider. It is the record.
"""

from __future__ import annotations

import json
import math
import os
import socket
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

# A session is one of these, and never two at once.
SESSION_STATES = ("active", "suspended", "closed", "lost")

# Who is beating for a turn. Host and pid alone are not enough: a pid is
# reused, and a reused pid holding a dead process's lease would read as
# the same owner still alive. The random tail is per interpreter, so a new
# process is never mistaken for the one that died.
PROCESS_OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

# A lease goes stale after this many missed beats, never sooner than the
# floor: a garbage collection pause or a busy registry must not turn a
# live turn into a dead one. Stale means the beating stopped, never that
# the turn failed -- what the turn did is its terminal record's to say.
LEASE_STALE_BEATS = 3
LEASE_STALE_FLOOR_SEC = 30.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_sessions (
    session_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_session_id TEXT NOT NULL DEFAULT '',
    project_id TEXT NOT NULL DEFAULT 'default',
    customer_id TEXT NOT NULL DEFAULT '',
    environment_id TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    order_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'active',
    state_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seq INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_harness_sessions_run ON harness_sessions(run_id);
CREATE INDEX IF NOT EXISTS idx_harness_sessions_order ON harness_sessions(order_id);
CREATE TABLE IF NOT EXISTS harness_session_events (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    turn_id TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, seq)
);
-- Who holds a turn right now, and how soon they promised to say so
-- again. Not an event: the stream is gap-free and replayed on reconnect,
-- and a beat every few seconds would flood it. Not a column on
-- harness_sessions either: updated_at moves for every event, so it
-- cannot tell "still beating" from "wrote a progress line". At most one
-- row per session, dropped when the turn ends.
CREATE TABLE IF NOT EXISTS harness_turn_leases (
    session_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    beat_at TEXT NOT NULL,
    period_sec REAL NOT NULL DEFAULT 0
);
"""


class HarnessSessionLost(RuntimeError):
    """The Provider session cannot be continued, and must not be pretended into life."""


class HarnessSessionBusy(ValueError):
    """An unfinished turn exists; neither a timeout nor a restart releases it."""


class HarnessRequestDuplicate(ValueError):
    """A durable dispatch claim already exists; never replay it automatically."""


@dataclass
class HarnessSessionRecord:
    session_id: str
    provider: str
    provider_session_id: str = ""
    project_id: str = "default"
    customer_id: str = ""
    environment_id: str = ""
    run_id: str = ""
    order_id: str = ""
    task_id: str = ""
    state: str = "active"
    state_reason: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_seq: int = 0

    def to_payload(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class HarnessSessionEvent:
    session_id: str
    seq: int
    at: str
    event_type: str
    turn_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "seq": self.seq,
            "at": self.at,
            "event_type": self.event_type,
            "turn_id": self.turn_id,
            "data": dict(self.data),
        }


class HarnessSessionRegistry:
    """The durable mapping between our runs and a Provider's sessions."""

    def __init__(self, db_path: str | Path, *, lock_timeout: float = 1.0) -> None:
        if isinstance(lock_timeout, bool) or not isinstance(lock_timeout, (int, float)) or not math.isfinite(lock_timeout) or not 0 < lock_timeout <= 10:
            raise ValueError("lock_timeout must be finite and between 0 and 10 seconds")
        self.lock_timeout = float(lock_timeout)
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self, *, timeout: float | None = None) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=self.lock_timeout if timeout is None else float(timeout))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── sessions ──

    def open_session(
        self,
        *,
        provider: str,
        project_id: str = "default",
        customer_id: str = "",
        environment_id: str = "",
        run_id: str = "",
        order_id: str = "",
        task_id: str = "",
        provider_session_id: str = "",
        session_id: str = "",
        initial_context: dict[str, Any] | None = None,
    ) -> HarnessSessionRecord:
        clean_provider = str(provider or "").strip()
        if not clean_provider:
            raise ValueError("a harness session needs a provider")
        identifier = str(session_id or "").strip() or f"hs-{uuid.uuid4().hex[:12]}"
        initial_json = json.dumps(initial_context, ensure_ascii=False, sort_keys=True) if initial_context is not None else None
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO harness_sessions(session_id, provider, provider_session_id, project_id, customer_id, "
                "environment_id, run_id, order_id, task_id, state, created_at, updated_at, last_seq) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 0)",
                (
                    identifier, clean_provider, str(provider_session_id or ""), str(project_id or "default"),
                    str(customer_id or ""), str(environment_id or ""), str(run_id or ""), str(order_id or ""),
                    str(task_id or ""), now, now,
                ),
            )
            if initial_json is not None:
                conn.execute(
                    "INSERT INTO harness_session_events(session_id, seq, at, event_type, turn_id, data_json) "
                    "VALUES (?, 1, ?, 'session_created', '', ?)", (identifier, now, initial_json),
                )
                conn.execute("UPDATE harness_sessions SET last_seq = 1 WHERE session_id = ?", (identifier,))
        return self.session(identifier)  # type: ignore[return-value]

    def bind_provider_session(self, session_id: str, provider_session_id: str) -> HarnessSessionRecord:
        """Record the id the Provider gave back, once it has given one."""
        clean = str(provider_session_id or "").strip()
        if not clean:
            raise ValueError("provider_session_id is required")
        with self._connect() as conn:
            conn.execute(
                "UPDATE harness_sessions SET provider_session_id = ?, updated_at = ? WHERE session_id = ?",
                (clean, datetime.now().isoformat(), str(session_id)),
            )
        return self._require(session_id)

    def session(self, session_id: str) -> HarnessSessionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM harness_sessions WHERE session_id = ?", (str(session_id or ""),)
            ).fetchone()
        return self._record(row) if row is not None else None

    def sessions_for_run(self, run_id: str) -> list[HarnessSessionRecord]:
        return self._query("run_id", run_id)

    def sessions_for_order(self, order_id: str) -> list[HarnessSessionRecord]:
        return self._query("order_id", order_id)

    def sessions_for_task(self, task_id: str) -> list[HarnessSessionRecord]:
        """Sessions bound to one task, oldest first.

        The console and the master conversation hold a task id; run and order
        ids belong to layers below them. Without this the only way to reach a
        session from a task was to read every session and filter, which is the
        kind of thing each caller writes slightly differently.

        An empty id finds nothing rather than every session that has no task.
        A bare column match would return exactly the sessions belonging to no
        task at all, which is the opposite of what a lookup by task means.
        """
        clean = str(task_id or "").strip()
        if not clean:
            return []
        return self._query("task_id", clean)

    def _query(self, column: str, value: str) -> list[HarnessSessionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM harness_sessions WHERE {column} = ? ORDER BY created_at", (str(value or ""),)
            ).fetchall()
        return [self._record(row) for row in rows]

    @staticmethod
    def _pending_turn(conn: sqlite3.Connection, session_id: str, turn_id: str = "") -> bool:
        return conn.execute(
            "SELECT 1 FROM harness_session_events s WHERE s.session_id = ? "
            "AND s.event_type = 'turn_started' AND s.turn_id != '' AND NOT EXISTS ("
            "SELECT 1 FROM harness_session_events e WHERE e.session_id = s.session_id "
            "AND e.turn_id = s.turn_id AND e.seq > s.seq "
            "AND e.event_type IN ('turn_finished', 'turn_failed', 'turn_cancelled')) "
            "AND (? = '' OR s.turn_id = ?) LIMIT 1",
            (str(session_id), turn_id, turn_id),
        ).fetchone() is not None

    def pending_turn_id(self, session_id: str) -> str:
        """The turn whose outcome this registry does not know, or ''."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT s.turn_id FROM harness_session_events s WHERE s.session_id = ? "
                "AND s.event_type = 'turn_started' AND s.turn_id != '' AND NOT EXISTS ("
                "SELECT 1 FROM harness_session_events e WHERE e.session_id = s.session_id "
                "AND e.turn_id = s.turn_id AND e.seq > s.seq "
                "AND e.event_type IN ('turn_finished', 'turn_failed', 'turn_cancelled')) "
                "ORDER BY s.seq LIMIT 1",
                (str(session_id),),
            ).fetchone()
        return str(row["turn_id"]) if row is not None else ""

    def interrupt_requested_after(self, session_id: str, seq: int, *, turn_id: str | None = None) -> bool:
        """Whether anybody asked for this session's turn to stop since event ``seq``."""
        with self._connect() as conn:
            turn_clause = " AND turn_id = ?" if turn_id is not None else ""
            params = (str(session_id), int(seq)) if turn_id is None else (str(session_id), int(seq), str(turn_id))
            row = conn.execute(
                "SELECT 1 FROM harness_session_events WHERE session_id = ? AND seq > ? "
                "AND event_type = 'interrupt_requested'" + turn_clause + " LIMIT 1",
                params,
            ).fetchone()
        return row is not None

    def request_interrupt(self, session_id: str, *, actor: str, expected_turn_id: str = "") -> str:
        """Ask, durably, for the pending turn to stop; return its id, or '' when nothing is pending.

        The registry cannot stop anything. The process running the turn
        reads this at its next progress event and cancels itself; a turn
        that emits no progress notices nothing until it ends. The request
        is a fact about who asked, not a claim that anything stopped.
        """
        turn_id = self.pending_turn_id(session_id)
        if not turn_id:
            return ""
        if expected_turn_id and turn_id != expected_turn_id:
            raise HarnessSessionBusy("the requested turn changed; refresh before interrupting")
        self.record_event(
            session_id, event_type="interrupt_requested", turn_id=turn_id,
            data={"actor": str(actor or ""), "cross_process": True},
        )
        return turn_id

    def settle_pending_turn(self, session_id: str, *, reason: str, actor: str, expected_turn_id: str = "") -> str:
        """A person closes a turn whose terminal record never landed.

        Recorded as ``turn_failed`` with who settled it and why, never as a
        pass: nothing here knows what the turn did. The session is suspended
        so the next turn is a deliberate resume, not a replay.
        """
        if not str(actor or "").strip():
            raise ValueError("settling an unknown turn needs a named actor")
        turn_id = self.pending_turn_id(session_id)
        if not turn_id:
            return ""
        if expected_turn_id and turn_id != expected_turn_id:
            raise HarnessSessionBusy("the requested turn changed; refresh before settling")
        self.record_event(
            session_id, event_type="turn_failed", turn_id=turn_id,
            data={"settled_by": str(actor), "reason": str(reason or "")[:500], "outcome_known": False},
            state_after="suspended", state_reason=f"turn settled by {actor} without a known outcome",
            settle_turn=True,
        )
        return turn_id

    def beat_turn(self, session_id: str, *, turn_id: str, period_sec: float, owner: str = "") -> bool:
        """Say that a process still holds this turn, and how soon it will say so again.

        Written only while the turn is still the session's unsettled
        claim, so a beat that arrives after somebody settled it cannot
        resurrect it; that case answers False.

        The row claims a process, never an outcome. A beating process may
        still be stuck and a silent one may still be working -- this is
        only the difference between "running somewhere else" and "the
        process that had it is gone".
        """
        clean_turn = str(turn_id or "").strip()
        if not clean_turn:
            return False
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._pending_turn(conn, session_id, clean_turn):
                return False
            conn.execute(
                "INSERT INTO harness_turn_leases(session_id, turn_id, owner, beat_at, period_sec) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "turn_id = excluded.turn_id, owner = excluded.owner, beat_at = excluded.beat_at, "
                "period_sec = excluded.period_sec",
                (
                    str(session_id), clean_turn, str(owner or PROCESS_OWNER),
                    datetime.now().isoformat(), max(0.0, float(period_sec or 0.0)),
                ),
            )
        return True

    def release_turn_lease(self, session_id: str, *, turn_id: str = "") -> None:
        """Drop the lease without ending the turn: the holder is letting go."""
        with self._connect() as conn:
            if str(turn_id or "").strip():
                conn.execute(
                    "DELETE FROM harness_turn_leases WHERE session_id = ? AND turn_id = ?",
                    (str(session_id), str(turn_id).strip()),
                )
            else:
                conn.execute("DELETE FROM harness_turn_leases WHERE session_id = ?", (str(session_id),))

    def pending_turns(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Every session with a turn whose outcome this registry does not know.

        What a person sees before settling one: whose run, which task,
        when it started, and what the process holding it last said about
        itself. ``liveness`` is one of:

        ``elsewhere``  a process beat for it recently and has not stopped.
        ``stale``      a process took it and then stopped beating. Probably
                       gone -- still no evidence of what the turn did.
        ``unknown``    nobody ever beat for it: an older turn, or a Provider
                       that holds no lease. Unknown is not dead, and nothing
                       reading this may present it as dead.

        The verdict is a clock comparison against a cadence the holder
        wrote down itself, so a slow Provider is not called dead for
        being slow.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT h.session_id, h.provider, h.project_id, h.run_id, h.order_id, h.task_id, h.state, "
                "CASE WHEN EXISTS (SELECT 1 FROM harness_session_events u "
                "WHERE u.session_id = s.session_id AND u.turn_id = s.turn_id AND u.seq > s.seq "
                "AND u.event_type = 'turn_dispatch_unknown') THEN 'dispatch_unknown' "
                "ELSE 'unsettled' END AS outcome_status, "
                "s.turn_id, s.at AS started_at, l.owner AS lease_owner, l.beat_at AS lease_beat_at, "
                "l.period_sec AS lease_period_sec FROM harness_session_events s "
                "JOIN harness_sessions h ON h.session_id = s.session_id "
                "LEFT JOIN harness_turn_leases l ON l.session_id = s.session_id AND l.turn_id = s.turn_id "
                "WHERE s.event_type = 'turn_started' AND s.turn_id != '' AND NOT EXISTS ("
                "SELECT 1 FROM harness_session_events e WHERE e.session_id = s.session_id "
                "AND e.turn_id = s.turn_id AND e.seq > s.seq "
                "AND e.event_type IN ('turn_finished', 'turn_failed', 'turn_cancelled')) "
                "ORDER BY s.at, s.session_id"
            ).fetchall()
        moment = now or datetime.now()
        out: list[dict[str, Any]] = []
        for row in rows:
            item: dict[str, Any] = {
                key: ("" if row[key] is None else str(row[key])) for key in row.keys()
            }
            item["lease_period_sec"] = float(row["lease_period_sec"] or 0.0)
            item["lease_owner_is_this_process"] = (
                bool(row["lease_owner"]) and str(row["lease_owner"]) == PROCESS_OWNER
            )
            item["beat_age_sec"] = None
            item["stale_after_sec"] = None
            item["liveness"] = "unknown"
            beat_at = str(row["lease_beat_at"] or "")
            if beat_at:
                try:
                    beat = datetime.fromisoformat(beat_at)
                except ValueError:
                    # An unreadable beat is not a dead process; it is a
                    # beat we cannot read, and unknown is the honest word.
                    out.append(item)
                    continue
                age = max(0.0, (moment - beat).total_seconds())
                limit = max(LEASE_STALE_FLOOR_SEC, LEASE_STALE_BEATS * item["lease_period_sec"])
                item["beat_age_sec"] = round(age, 3)
                item["stale_after_sec"] = round(limit, 3)
                item["liveness"] = "elsewhere" if age <= limit else "stale"
            out.append(item)
        return out

    def set_state(self, session_id: str, state: str, *, reason: str = "", require_idle: bool = False) -> HarnessSessionRecord:
        if state not in SESSION_STATES:
            raise ValueError(f"unknown session state {state!r}")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM harness_sessions WHERE session_id = ?", (str(session_id),)).fetchone()
            if row is None:
                raise ValueError(f"harness session {session_id!r} not found")
            record = self._record(row)
            if require_idle and self._pending_turn(conn, session_id):
                raise HarnessSessionBusy("an unfinished turn requires owner settlement before changing session state")
            if record.state in {"closed", "lost"} and state in {"active", "suspended"}:
                raise HarnessSessionLost(f"session {session_id!r} was {record.state}; start a new one")
            conn.execute(
                "UPDATE harness_sessions SET state = ?, state_reason = ?, updated_at = ? WHERE session_id = ?",
                (state, str(reason or ""), datetime.now().isoformat(), str(session_id)),
            )
        return self._require(session_id)

    def bind_evaluation_session(
        self,
        session_id: str,
        *,
        spec_fingerprint: str,
        asset_fingerprint: str,
        variant: str,
    ) -> HarnessSessionEvent:
        """Bind an idle session to one frozen RSI side before its first turn.

        This is deliberately an event, not a mutable session label.  A run
        selected after its outcome is known is not an experiment; refusing a
        late or second binding prevents that form of evidence cherry-picking.
        """

        return self.bind_evaluation_sessions(
            [session_id], spec_fingerprint=spec_fingerprint,
            asset_fingerprint=asset_fingerprint, variant=variant,
        )[0]

    def bind_evaluation_sessions(
        self,
        session_ids: list[str] | tuple[str, ...],
        *,
        spec_fingerprint: str,
        asset_fingerprint: str,
        variant: str,
    ) -> list[HarnessSessionEvent]:
        """Bind a complete sample set atomically, or bind none of it."""

        spec = str(spec_fingerprint or "").strip().lower()
        asset = str(asset_fingerprint or "").strip().lower()
        side = str(variant or "").strip().lower()
        if len(spec) != 64 or any(char not in "0123456789abcdef" for char in spec):
            raise ValueError("evaluation binding requires a sha256 spec fingerprint")
        if len(asset) != 64 or any(char not in "0123456789abcdef" for char in asset):
            raise ValueError("evaluation binding requires a sha256 asset fingerprint")
        if side not in {"baseline", "candidate"}:
            raise ValueError("evaluation variant must be baseline or candidate")
        identifiers = [str(value or "").strip() for value in session_ids]
        if not identifiers or len(identifiers) > 1000 or any(not value for value in identifiers):
            raise ValueError("evaluation binding needs 1-1000 named sessions")
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("evaluation session ids must be unique")
        now = datetime.now().isoformat()
        data = {
            "spec_fingerprint": spec,
            "asset_fingerprint": asset,
            "variant": side,
        }
        events: list[HarnessSessionEvent] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows: dict[str, sqlite3.Row] = {}
            for session_id in identifiers:
                row = conn.execute(
                    "SELECT state, last_seq FROM harness_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"harness session {session_id!r} not found")
                if str(row["state"] or "") != "active":
                    raise ValueError("only an active idle session can be bound for evaluation")
                if conn.execute(
                    "SELECT 1 FROM harness_session_events WHERE session_id = ? "
                    "AND event_type = 'turn_started' LIMIT 1", (session_id,),
                ).fetchone() is not None:
                    raise ValueError("evaluation binding must happen before the first turn")
                if conn.execute(
                    "SELECT 1 FROM harness_session_events WHERE session_id = ? "
                    "AND event_type = 'evaluation_bound' LIMIT 1", (session_id,),
                ).fetchone() is not None:
                    raise ValueError("harness session already has an evaluation binding")
                rows[session_id] = row
            for session_id in identifiers:
                seq = int(rows[session_id]["last_seq"] or 0) + 1
                conn.execute(
                    "INSERT INTO harness_session_events(session_id, seq, at, event_type, turn_id, data_json) "
                    "VALUES (?, ?, ?, 'evaluation_bound', '', ?)",
                    (session_id, seq, now, json.dumps(data, ensure_ascii=False, sort_keys=True)),
                )
                conn.execute(
                    "UPDATE harness_sessions SET last_seq = ?, updated_at = ? WHERE session_id = ?",
                    (seq, now, session_id),
                )
                events.append(HarnessSessionEvent(session_id, seq, now, "evaluation_bound", "", dict(data)))
        return events

    # ── events ──

    def record_event(
        self,
        session_id: str,
        *,
        event_type: str,
        turn_id: str = "",
        data: dict[str, Any] | None = None,
        exclusive_turn: bool = False,
        settle_turn: bool = False,
        state_after: str = "",
        state_reason: str = "",
        lock_timeout: float | None = None,
    ) -> HarnessSessionEvent:
        """Append one event and hand back its sequence number.

        ``lock_timeout`` lets one write wait longer than the registry's
        default: a terminal record settles the claim that refuses replay,
        and giving up on it after the ordinary second wedges the session.

        The sequence is allocated inside the same immediate transaction that
        writes the row, so two writers cannot be given the same number and a
        reader asking for "everything after 41" cannot miss one.
        """
        if exclusive_turn and (event_type != "turn_started" or not turn_id):
            raise ValueError("exclusive_turn needs a turn_started event and turn id")
        if settle_turn and (not turn_id or event_type not in {"turn_finished", "turn_failed", "turn_cancelled"}):
            raise ValueError("settle_turn needs a terminal event and turn id")
        if state_after and (state_after != "suspended" or event_type not in {"turn_finished", "turn_failed", "turn_cancelled", "turn_dispatch_unknown"}):
            raise ValueError("only a terminal or dispatch-unknown turn may atomically suspend a session")
        if event_type == "turn_dispatch_unknown" and not turn_id:
            raise ValueError("dispatch-unknown requires its pending turn id")
        if lock_timeout is not None and (
            isinstance(lock_timeout, bool) or not isinstance(lock_timeout, (int, float))
            or not math.isfinite(lock_timeout) or not 0 < lock_timeout <= 10
        ):
            raise ValueError("lock_timeout must be finite and between 0 and 10 seconds")
        now = datetime.now().isoformat()
        with self._connect(timeout=lock_timeout) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, last_seq FROM harness_sessions WHERE session_id = ?", (str(session_id),)
            ).fetchone()
            if row is None:
                raise ValueError(f"harness session {session_id!r} not found")
            if event_type == "steer_requested":
                request_id = str((data or {}).get("request_id", ""))
                if not request_id or len(request_id) > 128 or not turn_id:
                    raise ValueError("steering requires a bounded request id and a turn id")
                prior = conn.execute(
                    "SELECT turn_id, data_json FROM harness_session_events "
                    "WHERE session_id = ? AND event_type = 'steer_requested' "
                    "AND json_extract(data_json, '$.request_id') = ? LIMIT 1",
                    (str(session_id), request_id),
                ).fetchone()
                if prior is not None:
                    previous = json.loads(prior["data_json"])
                    if (prior["turn_id"] != turn_id
                            or previous.get("fingerprint") != (data or {}).get("fingerprint")):
                        raise ValueError("request id was already used for a different instruction or turn")
                    raise HarnessRequestDuplicate("steering dispatch already claimed; inspect its receipt, do not resend")
                if not self._pending_turn(conn, session_id, turn_id):
                    raise HarnessSessionBusy("the requested steering turn is no longer pending")
            if event_type == "dynamic_tool_claimed":
                call_id = (data or {}).get("call_id", "")
                fingerprint = (data or {}).get("fingerprint", "")
                if (not isinstance(call_id, str) or not 1 <= len(call_id) <= 256
                        or not isinstance(fingerprint, str) or len(fingerprint) != 64
                        or any(c not in "0123456789abcdef" for c in fingerprint) or not turn_id):
                    raise ValueError("dynamic tool requires a bounded call id and fingerprint")
                if row["state"] != "active" or not self._pending_turn(conn, session_id, turn_id):
                    raise HarnessSessionBusy("dynamic tool no longer belongs to an active turn")
                prior = conn.execute(
                    "SELECT 1 FROM harness_session_events WHERE session_id = ? AND turn_id = ? "
                    "AND event_type = 'dynamic_tool_claimed' AND json_extract(data_json, '$.call_id') = ? LIMIT 1",
                    (str(session_id), str(turn_id), call_id),
                ).fetchone()
                if prior is not None:
                    raise HarnessRequestDuplicate("dynamic tool already claimed; do not replay")
            if event_type == "model_request_claimed":
                fingerprint = (data or {}).get("fingerprint", "")
                if (not isinstance(fingerprint, str) or len(fingerprint) != 64
                        or any(c not in "0123456789abcdef" for c in fingerprint) or not turn_id):
                    raise ValueError("model request requires its fingerprint and bound turn")
                if row["state"] != "active" or not self._pending_turn(conn, session_id, turn_id):
                    raise HarnessSessionBusy("model route no longer belongs to an active pending turn")
                prior = conn.execute(
                    "SELECT 1 FROM harness_session_events WHERE session_id = ? AND turn_id = ? "
                    "AND event_type = 'model_request_claimed' AND json_extract(data_json, '$.fingerprint') = ? LIMIT 1",
                    (str(session_id), str(turn_id), fingerprint),
                ).fetchone()
                if prior is not None:
                    raise HarnessRequestDuplicate("model request already claimed; automatic replay refused")
            if row["state"] == "closed":
                raise ValueError(f"session {session_id!r} is closed")
            if event_type == "turn_dispatch_unknown" and not self._pending_turn(conn, session_id, turn_id):
                raise HarnessSessionBusy("dispatch-unknown turn is no longer pending; cannot suspend another turn")
            if settle_turn and not self._pending_turn(conn, session_id, turn_id):
                raise HarnessSessionBusy("this turn has no unsettled claim; cannot settle another owner's turn")
            if event_type == "interrupt_requested" and turn_id and not self._pending_turn(conn, session_id, turn_id):
                raise HarnessSessionBusy("the requested turn is no longer pending; refresh before interrupting")
            if exclusive_turn:
                if row["state"] != "active":
                    raise ValueError("only an active session can start a turn")
                if self._pending_turn(conn, session_id):
                    raise HarnessSessionBusy("a turn is already running or its outcome is unknown; automatic replay refused")
            seq = int(row["last_seq"]) + 1
            conn.execute(
                "INSERT INTO harness_session_events(session_id, seq, at, event_type, turn_id, data_json) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (str(session_id), seq, now, str(event_type), str(turn_id or ""),
                 json.dumps(dict(data or {}), ensure_ascii=False, sort_keys=True)),
            )
            conn.execute(
                "UPDATE harness_sessions SET last_seq = ?, updated_at = ? WHERE session_id = ?",
                (seq, now, str(session_id)),
            )
            if event_type in {"turn_finished", "turn_failed", "turn_cancelled"} and turn_id:
                # The turn ended, so its lease has nothing left to hold.
                # Dropped inside the same transaction as the terminal
                # record, so no reader sees an ended turn still beating.
                conn.execute(
                    "DELETE FROM harness_turn_leases WHERE session_id = ? AND turn_id = ?",
                    (str(session_id), str(turn_id)),
                )
            if state_after:
                conn.execute(
                    "UPDATE harness_sessions SET state = ?, state_reason = ? WHERE session_id = ?",
                    (state_after, state_reason, str(session_id)),
                )
        return HarnessSessionEvent(str(session_id), seq, now, str(event_type), str(turn_id or ""), dict(data or {}))

    # The rows that make up a turn's action ledger, and the ones that end a turn.
    _ACTION_EVENTS = ("item_started", "item_completed", "dynamic_tool_claimed", "dynamic_tool_returned", "approval_answered")
    _TURN_ENDS = ("turn_finished", "turn_failed", "turn_cancelled")
    # Items that are the provider talking or thinking, not doing.
    _NOT_ACTIONS = {"agentMessage", "reasoning", "userMessage", "plan", "item", ""}

    def turn_actions(self, session_id: str, turn_id: str) -> dict[str, Any]:
        """What the provider said it did in one turn, in order, and which of it finished. A read; nothing is settled.

        ``completed`` means the provider REPORTED the action finished (its status and exit code are its own words,
        never something we verified). ``in_flight`` means a start is on record and no completion. A tool of ours that
        was claimed and never returned is ``outcome_unknown``: it may have written. When the turn has no end on record
        the list closes with ``unknown_after`` -- whatever the provider did after the last durable row was never
        written down, and the absence of a row is not the absence of an action.
        """
        rows: list[HarnessSessionEvent] = []
        seq, complete = 0, True
        while True:
            page = self.events_after(session_id, seq, limit=2000)
            rows.extend(row for row in page if row.turn_id == str(turn_id or ""))
            if len(page) < 2000:
                break
            seq = page[-1].seq
            if len(rows) > 20000:
                complete = False
                break
        order: list[str] = []
        actions: dict[str, dict[str, Any]] = {}
        ended, last_seq, unpaired = "", 0, 0
        for row in rows:
            last_seq = max(last_seq, row.seq)
            if row.event_type in self._TURN_ENDS:
                ended = row.event_type
                continue
            if row.event_type not in self._ACTION_EVENTS:
                continue
            data = dict(row.data or {})
            if row.event_type in ("item_started", "item_completed"):
                if str(data.get("item_type", "") or "") in self._NOT_ACTIONS:
                    continue
                key = "item:" + str(data.get("item_id", "") or "")
                if key == "item:":
                    # A row written before the ledger existed names no item: it cannot be paired, and is not guessed at.
                    unpaired += 1
                    continue
                described = {k: v for k, v in data.items() if k not in {"method"}}
                entry = actions.setdefault(key, {"kind": str(data.get("item_type", "")), "state": "in_flight", "first_seq": row.seq})
                entry.update(described)
                if row.event_type == "item_completed":
                    entry.update({"state": "completed", "reported_by": "provider", "completed_seq": row.seq})
            elif row.event_type in ("dynamic_tool_claimed", "dynamic_tool_returned"):
                key = "tool:" + str(data.get("call_id", "") or "")
                entry = actions.setdefault(key, {"kind": "our_tool", "tool": str(data.get("tool", "")), "state": "outcome_unknown",
                                                 "first_seq": row.seq, "call_id": str(data.get("call_id", ""))})
                if row.event_type == "dynamic_tool_returned":
                    entry.update({"state": "completed", "reported_by": "this_runtime", "success": bool(data.get("success")),
                                  "completed_seq": row.seq})
            else:  # approval_answered
                key = f"approval:{row.seq}"
                entry = actions.setdefault(key, {"kind": "approval", "state": "completed", "reported_by": "this_runtime", "first_seq": row.seq})
                entry.update({k: v for k, v in data.items() if k in {"approval_id", "item_id", "decision", "approved"}})
            if key not in order:
                order.append(key)
        listed = [actions[key] for key in order]
        recorded = next((row for row in reversed(rows) if row.event_type == "workspace_changes"), None)
        baseline = next((row for row in rows if row.event_type == "workspace_baseline"), None)
        if recorded is not None:
            # What the workspace looked like after the turn, compared with before it: observed by a scan, not reported
            # by the provider, and it cannot tell the provider's writes from anybody else's in the same folder.
            listed.append({"kind": "workspace_changes", "state": "completed", "reported_by": "workspace_scan",
                           **{k: v for k, v in dict(recorded.data or {}).items() if k in {"changes", "total", "changes_truncated", "scan_complete"}}})
        if not ended:
            listed.append({"kind": "gap", "state": "unknown_after", "after_seq": last_seq,
                           "reason": "the turn has no end on record; what the provider did after the last row was never written down"})
        return {
            "session_id": str(session_id or ""), "turn_id": str(turn_id or ""), "turn_end": ended,
            "actions": listed,
            "ledger_complete": bool(ended) and complete and unpaired == 0,
            "unpaired_rows": unpaired,
            # Only when the turn never ended: the baseline a reader can compare the workspace against NOW.
            "baseline": dict(baseline.data or {}) if (baseline is not None and not ended and recorded is None) else None,
        }

    def events_after(self, session_id: str, seq: int = 0, *, limit: int = 500, event_type: str = "") -> list[HarnessSessionEvent]:
        """What a reconnecting client missed, in order."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ValueError("event page limit must be between 1 and 10000")
        clause = " AND event_type = ?" if event_type else ""
        params: list[Any] = [str(session_id or ""), int(seq)]
        if event_type:
            params.append(str(event_type))
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM harness_session_events WHERE session_id = ? AND seq > ?" + clause + " ORDER BY seq LIMIT ?",
                params,
            ).fetchall()
        out: list[HarnessSessionEvent] = []
        for row in rows:
            try:
                data = json.loads(row["data_json"] or "{}")
            except ValueError:
                data = {}
            out.append(HarnessSessionEvent(
                str(row["session_id"]), int(row["seq"]), str(row["at"]),
                str(row["event_type"]), str(row["turn_id"] or ""), data if isinstance(data, dict) else {},
            ))
        return out

    # ── helpers ──

    def _require(self, session_id: str) -> HarnessSessionRecord:
        record = self.session(session_id)
        if record is None:
            raise ValueError(f"harness session {session_id!r} not found")
        return record

    @staticmethod
    def _record(row: sqlite3.Row) -> HarnessSessionRecord:
        return HarnessSessionRecord(
            session_id=str(row["session_id"]),
            provider=str(row["provider"]),
            provider_session_id=str(row["provider_session_id"] or ""),
            project_id=str(row["project_id"] or "default"),
            customer_id=str(row["customer_id"] or ""),
            environment_id=str(row["environment_id"] or ""),
            run_id=str(row["run_id"] or ""),
            order_id=str(row["order_id"] or ""),
            task_id=str(row["task_id"] or ""),
            state=str(row["state"] or "active"),
            state_reason=str(row["state_reason"] or ""),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            last_seq=int(row["last_seq"] or 0),
        )
