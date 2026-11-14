"""SQLite effect journal with conservative crash-recovery semantics."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import (
    ApprovalType,
    EffectClaim,
    EffectConflictError,
    EffectEvent,
    EffectNotFoundError,
    EffectOutboxRecord,
    EffectRecord,
    EffectState,
    EffectVerification,
    InvalidEffectTransition,
    SimulationStatus,
    VerificationStatus,
)


_TERMINAL_STATES = {
    EffectState.APPLIED,
    EffectState.REJECTED,
    EffectState.FAILED,
    EffectState.OUTCOME_UNKNOWN,
}

_WORK_ITEM_PROJECTION_EVENTS = {
    "effect_approval_requested",
    "effect_applied",
    "effect_rejected",
    "effect_failed",
    "effect_outcome_unknown",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class EffectJournal:
    """A durable journal that can share the existing project SQLite file."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        if not parent.exists():
            raise FileNotFoundError(f"effect journal parent does not exist: {parent}")
        self._create_tables()

    def _connect(self) -> sqlite3.Connection:
        # Journal calls are dispatched off the asyncio event loop. Keep lock
        # waits bounded as well: contention is recoverable, a frozen control
        # plane is not.
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _create_tables(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS effects (
                    effect_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    state TEXT NOT NULL,
                    approval_type TEXT NOT NULL,
                    project_id TEXT NOT NULL DEFAULT 'default',
                    run_id TEXT NOT NULL DEFAULT '',
                    customer_id TEXT NOT NULL DEFAULT '',
                    environment_id TEXT NOT NULL DEFAULT '',
                    correlation_id TEXT NOT NULL DEFAULT '',
                    causation_id TEXT NOT NULL DEFAULT '',
                    provider_supports_idempotency INTEGER NOT NULL DEFAULT 0,
                    claim_before_apply INTEGER NOT NULL DEFAULT 1,
                    authorization_id TEXT NOT NULL DEFAULT '',
                    authorized_by TEXT NOT NULL DEFAULT '',
                    claim_token TEXT NOT NULL DEFAULT '',
                    claimed_by TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    provider_receipt TEXT NOT NULL DEFAULT '{}',
                    simulation_status TEXT NOT NULL DEFAULT 'unsupported',
                    simulation_details TEXT NOT NULL DEFAULT '{}',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    submitted_at TEXT NOT NULL DEFAULT '',
                    authorized_at TEXT NOT NULL DEFAULT '',
                    claimed_at TEXT NOT NULL DEFAULT '',
                    resolved_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(provider, kind, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS effect_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    effect_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_state TEXT NOT NULL DEFAULT '',
                    to_state TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '{}',
                    correlation_id TEXT NOT NULL DEFAULT '',
                    causation_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(effect_id) REFERENCES effects(effect_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS effect_outbox (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    outbox_id TEXT NOT NULL UNIQUE,
                    effect_event_id TEXT NOT NULL UNIQUE,
                    effect_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    published_at TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(effect_id) REFERENCES effects(effect_id) ON DELETE CASCADE,
                    FOREIGN KEY(effect_event_id) REFERENCES effect_events(event_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_effects_scope_state
                    ON effects(project_id, run_id, state);
                CREATE INDEX IF NOT EXISTS idx_effect_events_effect_created
                    ON effect_events(effect_id, created_at, event_id);
                CREATE INDEX IF NOT EXISTS idx_effect_outbox_status_sequence
                    ON effect_outbox(status, sequence);
                """
            )
            connection.commit()

    @staticmethod
    def _required(value: str, field_name: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError(f"{field_name} is required")
        return cleaned

    @staticmethod
    def _record(row: sqlite3.Row) -> EffectRecord:
        return EffectRecord(
            effect_id=str(row["effect_id"]),
            provider=str(row["provider"]),
            kind=str(row["kind"]),
            payload=json.loads(row["payload"] or "{}"),
            payload_hash=str(row["payload_hash"]),
            idempotency_key=str(row["idempotency_key"]),
            state=EffectState(str(row["state"])),
            approval_type=ApprovalType(str(row["approval_type"])),
            project_id=str(row["project_id"]),
            run_id=str(row["run_id"]),
            customer_id=str(row["customer_id"]),
            environment_id=str(row["environment_id"]),
            correlation_id=str(row["correlation_id"]),
            causation_id=str(row["causation_id"]),
            provider_supports_idempotency=bool(row["provider_supports_idempotency"]),
            claim_before_apply=bool(row["claim_before_apply"]),
            authorization_id=str(row["authorization_id"]),
            authorized_by=str(row["authorized_by"]),
            claim_token=str(row["claim_token"]),
            claimed_by=str(row["claimed_by"]),
            attempt_count=int(row["attempt_count"]),
            provider_receipt=json.loads(row["provider_receipt"] or "{}"),
            simulation_status=SimulationStatus(str(row["simulation_status"])),
            simulation_details=json.loads(row["simulation_details"] or "{}"),
            last_error=str(row["last_error"]),
            created_at=str(row["created_at"]),
            submitted_at=str(row["submitted_at"]),
            authorized_at=str(row["authorized_at"]),
            claimed_at=str(row["claimed_at"]),
            resolved_at=str(row["resolved_at"]),
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> EffectEvent:
        return EffectEvent(
            sequence=int(row["sequence"]),
            event_id=str(row["event_id"]),
            effect_id=str(row["effect_id"]),
            event_type=str(row["event_type"]),
            from_state=str(row["from_state"]),
            to_state=str(row["to_state"]),
            payload=json.loads(row["payload"] or "{}"),
            correlation_id=str(row["correlation_id"]),
            causation_id=str(row["causation_id"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _outbox_record(row: sqlite3.Row) -> EffectOutboxRecord:
        return EffectOutboxRecord(
            sequence=int(row["sequence"]),
            outbox_id=str(row["outbox_id"]),
            effect_event_id=str(row["effect_event_id"]),
            effect_id=str(row["effect_id"]),
            event_type=str(row["event_type"]),
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            last_error=str(row["last_error"]),
            created_at=str(row["created_at"]),
            published_at=str(row["published_at"]),
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        record: EffectRecord,
        event_type: str,
        *,
        from_state: str,
        to_state: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        created_at = _utc_now()
        connection.execute(
            """INSERT INTO effect_events
               (event_id, effect_id, event_type, from_state, to_state, payload,
                correlation_id, causation_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                record.effect_id,
                event_type,
                from_state,
                to_state,
                _canonical_json(payload or {}),
                record.correlation_id,
                record.causation_id,
                created_at,
            ),
        )
        metadata = dict(record.payload.get("metadata", {}) or {})
        work_item_id = str(metadata.get("work_item_id") or "").strip()
        if event_type in _WORK_ITEM_PROJECTION_EVENTS and work_item_id:
            connection.execute(
                """INSERT INTO effect_outbox
                   (outbox_id, effect_event_id, effect_id, event_type, status,
                    attempt_count, last_error, created_at, published_at)
                   VALUES (?, ?, ?, ?, 'pending', 0, '', ?, '')""",
                (
                    f"effect-outbox:{event_id}",
                    event_id,
                    record.effect_id,
                    event_type,
                    created_at,
                ),
            )
        return event_id

    @staticmethod
    def _load(connection: sqlite3.Connection, effect_id: str) -> EffectRecord:
        row = connection.execute(
            "SELECT * FROM effects WHERE effect_id = ?", (effect_id,)
        ).fetchone()
        if row is None:
            raise EffectNotFoundError(f"effect not found: {effect_id}")
        return EffectJournal._record(row)

    def get(self, effect_id: str) -> EffectRecord:
        with closing(self._connect()) as connection:
            return self._load(connection, effect_id)

    def list_effects(self, *, kind: str = "", limit: int = 500) -> list[EffectRecord]:
        """Recent effects, newest first, optionally of one kind. A read for audit views; nothing is claimed."""
        clean_kind = str(kind or "").strip()
        with closing(self._connect()) as connection:
            if clean_kind:
                rows = connection.execute("SELECT * FROM effects WHERE kind = ? ORDER BY created_at DESC LIMIT ?",
                                          (clean_kind, int(limit))).fetchall()
            else:
                rows = connection.execute("SELECT * FROM effects ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [self._record(row) for row in rows]

    def list_events(self, effect_id: str) -> list[EffectEvent]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM effect_events
                   WHERE effect_id = ? ORDER BY sequence""",
                (effect_id,),
            ).fetchall()
        return [self._event(row) for row in rows]

    @staticmethod
    def _execution_scope(connection, effect_id: str) -> dict[str, Any] | None:
        rows = connection.execute(
            "SELECT payload FROM effect_events WHERE effect_id = ? AND event_type = ?",
            (effect_id, "effect_proposed"),
        ).fetchall()
        if len(rows) != 1:
            return None
        try:
            value = json.loads(rows[0][0]).get("server_execution_scope")
        except (ValueError, TypeError, AttributeError):
            return None
        return value if isinstance(value, dict) and value else None

    def execution_scope(self, effect_id: str) -> dict[str, Any] | None:
        """Read server authority metadata from the original immutable event.

        It is separate from the provider's business payload and payload hash.
        Missing legacy metadata cannot be promoted by replaying a proposal.
        """
        with closing(self._connect()) as connection:
            return self._execution_scope(connection, effect_id)

    def latest_verification(self, effect_id: str) -> EffectVerification | None:
        """Read the most recent post-apply verification evidence.

        Verification is intentionally projected from the immutable event
        stream rather than stored as mutable columns on ``effects``.  A
        missing event means the effect has not been verified yet.
        """

        clean_effect_id = self._required(effect_id, "effect_id")
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM effect_events
                   WHERE effect_id = ? AND event_type = 'effect_verified'
                   ORDER BY sequence DESC LIMIT 1""",
                (clean_effect_id,),
            ).fetchone()
        if row is None:
            return None
        event = self._event(row)
        status = VerificationStatus(
            str(event.payload.get("status") or VerificationStatus.INCOMPLETE.value)
        )
        return EffectVerification(
            effect_id=clean_effect_id,
            status=status,
            details=dict(event.payload.get("details", {}) or {}),
            event_id=event.event_id,
            verified_at=event.created_at,
        )

    def get_event(self, event_id: str) -> EffectEvent:
        clean_event_id = self._required(event_id, "event_id")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM effect_events WHERE event_id = ?",
                (clean_event_id,),
            ).fetchone()
        if row is None:
            raise EffectNotFoundError(f"effect event not found: {clean_event_id}")
        return self._event(row)

    def list_pending_outbox(
        self,
        *,
        effect_id: str = "",
        limit: int = 100,
    ) -> list[EffectOutboxRecord]:
        clean_effect_id = str(effect_id or "").strip()
        bounded_limit = max(1, min(int(limit or 100), 1000))
        query = "SELECT * FROM effect_outbox WHERE status = 'pending'"
        params: list[Any] = []
        if clean_effect_id:
            query += " AND effect_id = ?"
            params.append(clean_effect_id)
        query += " ORDER BY sequence LIMIT ?"
        params.append(bounded_limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._outbox_record(row) for row in rows]

    def mark_outbox_published(self, outbox_id: str) -> bool:
        clean_outbox_id = self._required(outbox_id, "outbox_id")
        with self._transaction() as connection:
            cursor = connection.execute(
                """UPDATE effect_outbox
                   SET status = 'published', published_at = ?, last_error = ''
                   WHERE outbox_id = ? AND status = 'pending'""",
                (_utc_now(), clean_outbox_id),
            )
            if cursor.rowcount == 1:
                return True
            row = connection.execute(
                "SELECT status FROM effect_outbox WHERE outbox_id = ?",
                (clean_outbox_id,),
            ).fetchone()
            return row is not None and str(row["status"]) == "published"

    def mark_outbox_no_consumer(self, outbox_id: str, reason: str) -> bool:
        """Nobody reads this event type, so stop calling it pending.

        Distinct from published, because nothing was delivered, and from a
        failure, because nobody is going to try again. A pending queue that
        keeps rows no reader exists for stops meaning "work waiting".
        """
        clean_outbox_id = self._required(outbox_id, "outbox_id")
        with self._transaction() as connection:
            cursor = connection.execute(
                """UPDATE effect_outbox
                   SET status = 'no_consumer', last_error = ?
                   WHERE outbox_id = ? AND status = 'pending'""",
                (str(reason or "").strip()[:2000], clean_outbox_id),
            )
            return cursor.rowcount == 1

    def list_pending_outbox_all(self, *, limit: int = 100) -> list[Any]:
        """Every pending row, whatever its type -- the dispatcher routes them."""
        return self.list_pending_outbox(limit=limit)

    def record_outbox_failure(self, outbox_id: str, error: str) -> bool:
        clean_outbox_id = self._required(outbox_id, "outbox_id")
        with self._transaction() as connection:
            cursor = connection.execute(
                """UPDATE effect_outbox
                   SET attempt_count = attempt_count + 1, last_error = ?
                   WHERE outbox_id = ? AND status = 'pending'""",
                (str(error or "").strip()[:2000], clean_outbox_id),
            )
            return cursor.rowcount == 1

    def propose(
        self,
        *,
        provider: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        approval_type: ApprovalType = ApprovalType.BUSINESS,
        project_id: str = "default",
        run_id: str = "",
        customer_id: str = "",
        environment_id: str = "",
        correlation_id: str = "",
        causation_id: str = "",
        provider_supports_idempotency: bool = False,
        claim_before_apply: bool = True,
        effect_id: str | None = None,
        server_execution_scope: dict[str, Any] | None = None,
    ) -> EffectRecord:
        clean_provider = self._required(provider, "provider")
        clean_kind = self._required(kind, "kind")
        clean_key = self._required(idempotency_key, "idempotency_key")
        clean_project = self._required(project_id, "project_id")
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        if not provider_supports_idempotency and not claim_before_apply:
            raise ValueError("non-idempotent providers require claim_before_apply")

        effect_payload = dict(payload)
        clean_scope = None
        if server_execution_scope is not None:
            if not isinstance(server_execution_scope, dict) or not server_execution_scope:
                raise ValueError("server_execution_scope must be a nonempty dict")
            scope_json = json.dumps(server_execution_scope, ensure_ascii=False, sort_keys=True, allow_nan=False)
            if len(scope_json.encode("utf-8")) > 16384:
                raise ValueError("server_execution_scope is too large")
            clean_scope = json.loads(scope_json)
            if clean_scope.get("project_id") != clean_project:
                raise ValueError("server_execution_scope crosses project")
        payload_json = _canonical_json(effect_payload)
        digest = _payload_hash(effect_payload)
        created_at = _utc_now()
        new_effect_id = str(effect_id or uuid.uuid4()).strip()

        with self._transaction() as connection:
            existing_row = connection.execute(
                """SELECT * FROM effects
                   WHERE provider = ? AND kind = ? AND idempotency_key = ?""",
                (clean_provider, clean_kind, clean_key),
            ).fetchone()
            if existing_row is not None:
                existing = self._record(existing_row)
                identity = (
                    existing.payload_hash,
                    existing.project_id,
                    existing.run_id,
                    existing.customer_id,
                    existing.environment_id,
                )
                requested = (
                    digest,
                    clean_project,
                    str(run_id or "").strip(),
                    str(customer_id or "").strip(),
                    str(environment_id or "").strip(),
                )
                if identity != requested:
                    raise EffectConflictError(
                        "idempotency key already belongs to a different effect payload or scope"
                    )
                if self._execution_scope(connection, existing.effect_id) != clean_scope:
                    raise EffectConflictError("idempotency key already belongs to a different server execution scope")
                return existing

            connection.execute(
                """INSERT INTO effects
                   (effect_id, provider, kind, payload, payload_hash, idempotency_key,
                    state, approval_type, project_id, run_id, customer_id,
                    environment_id, correlation_id, causation_id,
                    provider_supports_idempotency, claim_before_apply, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_effect_id,
                    clean_provider,
                    clean_kind,
                    payload_json,
                    digest,
                    clean_key,
                    EffectState.PROPOSED.value,
                    ApprovalType(approval_type).value,
                    clean_project,
                    str(run_id or "").strip(),
                    str(customer_id or "").strip(),
                    str(environment_id or "").strip(),
                    str(correlation_id or "").strip(),
                    str(causation_id or "").strip(),
                    int(provider_supports_idempotency),
                    int(claim_before_apply),
                    created_at,
                ),
            )
            record = self._load(connection, new_effect_id)
            self._append_event(
                connection,
                record,
                "effect_proposed",
                from_state="",
                to_state=EffectState.PROPOSED.value,
                payload={"payload_hash": digest, **({"server_execution_scope": clean_scope} if clean_scope is not None else {})},
            )
            return record

    def submit_for_approval(self, effect_id: str) -> EffectRecord:
        with self._transaction() as connection:
            record = self._load(connection, effect_id)
            if record.state == EffectState.AWAITING_APPROVAL or record.is_terminal:
                return record
            if record.state != EffectState.PROPOSED:
                raise InvalidEffectTransition(
                    f"cannot submit effect in state {record.state.value}"
                )
            now = _utc_now()
            connection.execute(
                """UPDATE effects SET state = ?, submitted_at = ?
                   WHERE effect_id = ? AND state = ?""",
                (
                    EffectState.AWAITING_APPROVAL.value,
                    now,
                    effect_id,
                    EffectState.PROPOSED.value,
                ),
            )
            updated = self._load(connection, effect_id)
            self._append_event(
                connection,
                updated,
                "effect_approval_requested",
                from_state=EffectState.PROPOSED.value,
                to_state=EffectState.AWAITING_APPROVAL.value,
            )
            return updated

    def authorize(
        self,
        effect_id: str,
        *,
        authorization_id: str,
        authorized_by: str,
    ) -> EffectRecord:
        clean_auth = self._required(authorization_id, "authorization_id")
        clean_actor = self._required(authorized_by, "authorized_by")
        with self._transaction() as connection:
            record = self._load(connection, effect_id)
            if record.authorization_id:
                if (
                    record.authorization_id == clean_auth
                    and record.authorized_by == clean_actor
                ):
                    return record
                raise EffectConflictError("effect already has a different authorization")
            if record.state != EffectState.AWAITING_APPROVAL:
                raise InvalidEffectTransition(
                    f"cannot authorize effect in state {record.state.value}"
                )
            connection.execute(
                """UPDATE effects
                   SET authorization_id = ?, authorized_by = ?, authorized_at = ?
                   WHERE effect_id = ? AND state = ? AND authorization_id = ''""",
                (
                    clean_auth,
                    clean_actor,
                    _utc_now(),
                    effect_id,
                    EffectState.AWAITING_APPROVAL.value,
                ),
            )
            updated = self._load(connection, effect_id)
            self._append_event(
                connection,
                updated,
                "effect_authorized",
                from_state=EffectState.AWAITING_APPROVAL.value,
                to_state=EffectState.AWAITING_APPROVAL.value,
                payload={
                    "authorization_id": clean_auth,
                    "authorized_by": clean_actor,
                    "approval_type": updated.approval_type.value,
                },
            )
            return updated

    def reject(self, effect_id: str, *, reason: str, rejected_by: str) -> EffectRecord:
        with self._transaction() as connection:
            record = self._load(connection, effect_id)
            if record.state == EffectState.REJECTED:
                return record
            if record.state != EffectState.AWAITING_APPROVAL or record.authorization_id:
                raise InvalidEffectTransition(
                    f"cannot reject effect in state {record.state.value}"
                )
            connection.execute(
                """UPDATE effects SET state = ?, last_error = ?, resolved_at = ?
                   WHERE effect_id = ? AND state = ? AND authorization_id = ''""",
                (
                    EffectState.REJECTED.value,
                    str(reason or "").strip(),
                    _utc_now(),
                    effect_id,
                    EffectState.AWAITING_APPROVAL.value,
                ),
            )
            updated = self._load(connection, effect_id)
            self._append_event(
                connection,
                updated,
                "effect_rejected",
                from_state=EffectState.AWAITING_APPROVAL.value,
                to_state=EffectState.REJECTED.value,
                payload={"reason": updated.last_error, "rejected_by": rejected_by},
            )
            return updated

    def expire_approval(self, effect_id: str, *, expires_at: str, expired_by: str = "system") -> EffectRecord:
        """Terminalize an approval that outlived its durable deadline.

        Authorization may already have been written when a process crashed,
        but a claimed effect has crossed the execution fence and is never
        rewritten as merely expired.
        """
        reason = f"approval_expired: {str(expires_at or '').strip()}"
        with self._transaction() as connection:
            record = self._load(connection, effect_id)
            if record.state == EffectState.REJECTED and record.last_error == reason:
                return record
            if record.state != EffectState.AWAITING_APPROVAL or record.claim_token:
                raise InvalidEffectTransition(f"cannot expire approval in state {record.state.value}")
            connection.execute(
                """UPDATE effects SET state = ?, last_error = ?, resolved_at = ?
                   WHERE effect_id = ? AND state = ? AND claim_token = ''""",
                (EffectState.REJECTED.value, reason, _utc_now(), effect_id, EffectState.AWAITING_APPROVAL.value),
            )
            updated = self._load(connection, effect_id)
            self._append_event(
                connection,
                updated,
                "effect_rejected",
                from_state=EffectState.AWAITING_APPROVAL.value,
                to_state=EffectState.REJECTED.value,
                payload={"reason": "approval_expired", "rejected_by": str(expired_by or "system"), "expires_at": str(expires_at or "")},
            )
            return updated

    def record_simulation(
        self,
        effect_id: str,
        *,
        status: SimulationStatus,
        details: dict[str, Any] | None = None,
    ) -> EffectRecord:
        simulation_status = SimulationStatus(status)
        with self._transaction() as connection:
            record = self._load(connection, effect_id)
            if record.state not in {EffectState.PROPOSED, EffectState.AWAITING_APPROVAL}:
                raise InvalidEffectTransition(
                    f"cannot simulate effect in state {record.state.value}"
                )
            connection.execute(
                """UPDATE effects SET simulation_status = ?, simulation_details = ?
                   WHERE effect_id = ?""",
                (
                    simulation_status.value,
                    _canonical_json(details or {}),
                    effect_id,
                ),
            )
            updated = self._load(connection, effect_id)
            self._append_event(
                connection,
                updated,
                "effect_simulated",
                from_state=record.state.value,
                to_state=record.state.value,
                payload={"status": simulation_status.value, "details": details or {}},
            )
            return updated

    def record_verification(
        self,
        effect_id: str,
        *,
        status: VerificationStatus,
        details: dict[str, Any] | None = None,
    ) -> EffectEvent:
        """Append immutable post-apply verification evidence.

        Verification is an event on the existing journal, not a new truth
        table. It never changes an already-applied effect back to a pending
        state and therefore needs no schema migration.
        """

        verification_status = VerificationStatus(status)
        with self._transaction() as connection:
            record = self._load(connection, effect_id)
            if record.state != EffectState.APPLIED:
                raise InvalidEffectTransition(
                    f"cannot verify effect in state {record.state.value}"
                )
            event_id = self._append_event(
                connection,
                record,
                "effect_verified",
                from_state=EffectState.APPLIED.value,
                to_state=EffectState.APPLIED.value,
                payload={
                    "status": verification_status.value,
                    "details": dict(details or {}),
                },
            )
            row = connection.execute(
                "SELECT * FROM effect_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - same-transaction invariant
                raise EffectNotFoundError(f"effect event not found: {event_id}")
            return self._event(row)

    def record_look_back(
        self,
        effect_id: str,
        *,
        status: VerificationStatus,
        details: dict[str, Any] | None = None,
    ) -> EffectRecord:
        """A look-back at an outcome_unknown effect (C10).

        Independent evidence that the effect happened (``complete``) resolves
        it as applied, with the evidence in its receipt and a verification
        event like any applied effect's. An explicit, attributed owner review
        that no input happened resolves it as failed, never applied. Anything
        weaker stays unknown. Never a claim, never a second apply.
        """

        verification_status = VerificationStatus(status)
        clean_details = dict(details or {})
        no_effect = verification_status is VerificationStatus.COMPLETE and clean_details.get("happened") is False
        if no_effect and (clean_details.get("source") != "owner_attestation"
                or not str(clean_details.get("requested_by", "") or "").strip()
                or len(str(clean_details.get("evidence_sha256", ""))) != 64):
            raise InvalidEffectTransition("non-application needs an explicit attributed owner receipt review")
        target_state = EffectState.FAILED if no_effect else EffectState.APPLIED
        with self._transaction() as connection:
            record = self._load(connection, effect_id)
            if record.state != EffectState.OUTCOME_UNKNOWN:
                raise InvalidEffectTransition(
                    f"a look-back is for outcome_unknown, not {record.state.value}"
                )
            if verification_status is not VerificationStatus.COMPLETE:
                self._append_event(
                    connection,
                    record,
                    "effect_look_back",
                    from_state=EffectState.OUTCOME_UNKNOWN.value,
                    to_state=EffectState.OUTCOME_UNKNOWN.value,
                    payload={"status": verification_status.value, "details": clean_details},
                )
                return record
            receipt = {**dict(record.provider_receipt or {}), "look_back": clean_details}
            connection.execute(
                """UPDATE effects SET state = ?, provider_receipt = ?, resolved_at = ?
                   WHERE effect_id = ? AND state = ?""",
                (
                    target_state.value,
                    _canonical_json(receipt),
                    _utc_now(),
                    effect_id,
                    EffectState.OUTCOME_UNKNOWN.value,
                ),
            )
            updated = self._load(connection, effect_id)
            self._append_event(
                connection,
                updated,
                "effect_resolved_by_look_back",
                from_state=EffectState.OUTCOME_UNKNOWN.value,
                to_state=target_state.value,
                payload={"status": verification_status.value, "details": clean_details},
            )
            self._append_event(
                connection,
                updated,
                "effect_non_application_verified" if no_effect else "effect_verified",
                from_state=target_state.value,
                to_state=target_state.value,
                payload={"status": verification_status.value, "details": {**clean_details, "look_back": True}},
            )
            return updated

    def claim(self, effect_id: str, *, claim_token: str, claimed_by: str) -> EffectClaim:
        clean_token = self._required(claim_token, "claim_token")
        clean_actor = self._required(claimed_by, "claimed_by")
        with self._transaction() as connection:
            record = self._load(connection, effect_id)
            if record.state == EffectState.APPLIED:
                return EffectClaim(False, record, "already_applied")
            if record.state == EffectState.CLAIMED:
                return EffectClaim(False, record, "already_claimed")
            if record.state != EffectState.AWAITING_APPROVAL:
                return EffectClaim(False, record, f"state_{record.state.value}")
            if not record.authorization_id:
                return EffectClaim(False, record, "authorization_required")

            cursor = connection.execute(
                """UPDATE effects
                   SET state = ?, claim_token = ?, claimed_by = ?, claimed_at = ?,
                       attempt_count = attempt_count + 1, last_error = ''
                   WHERE effect_id = ? AND state = ? AND authorization_id != ''""",
                (
                    EffectState.CLAIMED.value,
                    clean_token,
                    clean_actor,
                    _utc_now(),
                    effect_id,
                    EffectState.AWAITING_APPROVAL.value,
                ),
            )
            if cursor.rowcount != 1:
                current = self._load(connection, effect_id)
                return EffectClaim(False, current, "claim_lost")
            updated = self._load(connection, effect_id)
            self._append_event(
                connection,
                updated,
                "effect_claimed",
                from_state=EffectState.AWAITING_APPROVAL.value,
                to_state=EffectState.CLAIMED.value,
                payload={"claimed_by": clean_actor, "attempt": updated.attempt_count},
            )
            return EffectClaim(True, updated)

    def begin_dispatch(self, effect_id: str, *, claim_token: str, fence: Any = None) -> EffectClaim:
        """Record the single dispatch crossing for an exact active claim.

        Uses the existing journal transaction/events, not a second lease or
        delivery ledger. A duplicate caller with the same claim cannot enter
        the provider twice, including across processes. A crash after this
        event retains the existing orphan/unknown recovery semantics.
        """
        clean_token = self._required(claim_token, "claim_token")
        with self._transaction() as connection:
            record = self._load(connection, effect_id)
            if record.state != EffectState.CLAIMED or record.claim_token != clean_token or not record.is_authorized:
                return EffectClaim(False, record, "claim_lost")
            rows = connection.execute(
                "SELECT payload FROM effect_events WHERE effect_id = ? AND event_type = ?",
                (effect_id, "effect_dispatch_started"),
            ).fetchall()
            if any(json.loads(row[0]).get("claim_token") == clean_token for row in rows):
                return EffectClaim(False, record, "dispatch_already_started")
            if fence is not None:
                # Host authority facts read on this same BEGIN IMMEDIATE
                # connection: no writer can change them between this check
                # and the dispatch event below. Faults refuse, never allow.
                try:
                    refusal = fence(connection, record)
                except Exception:
                    refusal = "execution_guard_unavailable"
                if refusal is not None and not isinstance(refusal, str):
                    refusal = "execution_guard_invalid_result"
                if refusal:
                    return EffectClaim(False, record, str(refusal))
            self._append_event(
                connection, record, "effect_dispatch_started",
                from_state=EffectState.CLAIMED.value, to_state=EffectState.CLAIMED.value,
                payload={"claim_token": clean_token, "attempt": record.attempt_count},
            )
            return EffectClaim(True, record)

    def recover_orphaned_claim(
        self,
        effect_id: str,
        *,
        claim_token: str,
        claimed_by: str,
        orphaned_after_seconds: float,
        now: datetime | None = None,
    ) -> EffectClaim:
        clean_token = self._required(claim_token, "claim_token")
        clean_actor = self._required(claimed_by, "claimed_by")
        current_time = now or datetime.now(timezone.utc)
        with self._transaction() as connection:
            record = self._load(connection, effect_id)
            if record.state != EffectState.CLAIMED:
                return EffectClaim(False, record, f"state_{record.state.value}")
            claimed_at = datetime.fromisoformat(record.claimed_at)
            if claimed_at.tzinfo is None:
                claimed_at = claimed_at.replace(tzinfo=timezone.utc)
            cutoff = current_time - timedelta(seconds=max(0.0, orphaned_after_seconds))
            if claimed_at > cutoff:
                return EffectClaim(False, record, "claim_not_orphaned")

            if not record.provider_supports_idempotency:
                detail = "orphaned non-idempotent claim; provider outcome cannot be proven"
                connection.execute(
                    """UPDATE effects
                       SET state = ?, last_error = ?, resolved_at = ?
                       WHERE effect_id = ? AND state = ? AND claim_token = ?""",
                    (
                        EffectState.OUTCOME_UNKNOWN.value,
                        detail,
                        _utc_now(),
                        effect_id,
                        EffectState.CLAIMED.value,
                        record.claim_token,
                    ),
                )
                updated = self._load(connection, effect_id)
                self._append_event(
                    connection,
                    updated,
                    "effect_outcome_unknown",
                    from_state=EffectState.CLAIMED.value,
                    to_state=EffectState.OUTCOME_UNKNOWN.value,
                    payload={"reason": detail},
                )
                return EffectClaim(False, updated, "outcome_unknown")

            cursor = connection.execute(
                """UPDATE effects
                   SET claim_token = ?, claimed_by = ?, claimed_at = ?,
                       attempt_count = attempt_count + 1, last_error = ''
                   WHERE effect_id = ? AND state = ? AND claim_token = ?""",
                (
                    clean_token,
                    clean_actor,
                    current_time.isoformat(),
                    effect_id,
                    EffectState.CLAIMED.value,
                    record.claim_token,
                ),
            )
            if cursor.rowcount != 1:
                current = self._load(connection, effect_id)
                return EffectClaim(False, current, "recovery_lost")
            updated = self._load(connection, effect_id)
            self._append_event(
                connection,
                updated,
                "effect_claim_recovered",
                from_state=EffectState.CLAIMED.value,
                to_state=EffectState.CLAIMED.value,
                payload={"claimed_by": clean_actor, "attempt": updated.attempt_count},
            )
            return EffectClaim(True, updated)

    def release_claim(
        self,
        effect_id: str,
        *,
        claim_token: str,
        reason: str,
    ) -> EffectRecord:
        """Release an exact claim when the external-effect fence was not crossed."""

        clean_token = self._required(claim_token, "claim_token")
        with self._transaction() as connection:
            record = self._load(connection, effect_id)
            if record.state != EffectState.CLAIMED or record.claim_token != clean_token:
                raise InvalidEffectTransition(
                    "effect claim can be released only by its active exact claim"
                )
            dispatches = connection.execute(
                "SELECT payload FROM effect_events WHERE effect_id = ? AND event_type = ?",
                (effect_id, "effect_dispatch_started"),
            ).fetchall()
            if any(json.loads(row[0]).get("claim_token") == clean_token for row in dispatches):
                raise InvalidEffectTransition("a dispatched claim cannot be released as not-started")
            connection.execute(
                """UPDATE effects
                   SET state = ?, claim_token = '', claimed_by = '', claimed_at = '',
                       last_error = ?
                   WHERE effect_id = ? AND state = ? AND claim_token = ?""",
                (
                    EffectState.AWAITING_APPROVAL.value,
                    str(reason or "").strip(),
                    effect_id,
                    EffectState.CLAIMED.value,
                    clean_token,
                ),
            )
            updated = self._load(connection, effect_id)
            self._append_event(
                connection,
                updated,
                "effect_claim_released",
                from_state=EffectState.CLAIMED.value,
                to_state=EffectState.AWAITING_APPROVAL.value,
                payload={"reason": updated.last_error},
            )
            return updated

    def finish_applied(
        self,
        effect_id: str,
        *,
        claim_token: str,
        provider_receipt: dict[str, Any] | None = None,
    ) -> EffectRecord:
        return self._finish(
            effect_id,
            claim_token=claim_token,
            state=EffectState.APPLIED,
            event_type="effect_applied",
            provider_receipt=provider_receipt,
        )

    def finish_failed(self, effect_id: str, *, claim_token: str, error: str) -> EffectRecord:
        return self._finish(
            effect_id,
            claim_token=claim_token,
            state=EffectState.FAILED,
            event_type="effect_failed",
            error=error,
        )

    def mark_outcome_unknown(
        self, effect_id: str, *, claim_token: str, error: str
    ) -> EffectRecord:
        return self._finish(
            effect_id,
            claim_token=claim_token,
            state=EffectState.OUTCOME_UNKNOWN,
            event_type="effect_outcome_unknown",
            error=error,
        )

    def record_retryable_error(
        self, effect_id: str, *, claim_token: str, error: str
    ) -> EffectRecord:
        with self._transaction() as connection:
            record = self._load(connection, effect_id)
            if record.state != EffectState.CLAIMED or record.claim_token != claim_token:
                raise InvalidEffectTransition("retryable error requires the active exact claim")
            if not record.provider_supports_idempotency:
                raise InvalidEffectTransition(
                    "non-idempotent provider errors must become outcome_unknown"
                )
            connection.execute(
                "UPDATE effects SET last_error = ? WHERE effect_id = ?",
                (str(error or "").strip(), effect_id),
            )
            updated = self._load(connection, effect_id)
            self._append_event(
                connection,
                updated,
                "effect_retryable_error",
                from_state=EffectState.CLAIMED.value,
                to_state=EffectState.CLAIMED.value,
                payload={"error": updated.last_error},
            )
            return updated

    def _finish(
        self,
        effect_id: str,
        *,
        claim_token: str,
        state: EffectState,
        event_type: str,
        provider_receipt: dict[str, Any] | None = None,
        error: str = "",
    ) -> EffectRecord:
        if state not in _TERMINAL_STATES:
            raise ValueError(f"finish state must be terminal: {state.value}")
        clean_token = self._required(claim_token, "claim_token")
        with self._transaction() as connection:
            record = self._load(connection, effect_id)
            if record.state == state:
                return record
            if record.state != EffectState.CLAIMED or record.claim_token != clean_token:
                raise InvalidEffectTransition("effect can be finished only by its active exact claim")
            connection.execute(
                """UPDATE effects
                   SET state = ?, provider_receipt = ?, last_error = ?, resolved_at = ?
                   WHERE effect_id = ? AND state = ? AND claim_token = ?""",
                (
                    state.value,
                    _canonical_json(provider_receipt or {}),
                    str(error or "").strip(),
                    _utc_now(),
                    effect_id,
                    EffectState.CLAIMED.value,
                    clean_token,
                ),
            )
            updated = self._load(connection, effect_id)
            self._append_event(
                connection,
                updated,
                event_type,
                from_state=EffectState.CLAIMED.value,
                to_state=state.value,
                payload={
                    "provider_receipt": provider_receipt or {},
                    "error": updated.last_error,
                },
            )
            return updated
