"""Persistent host admission over the existing outbox and delivery fence.

This host-only API never accepts an admission, evidence reader, policy decision,
or authorization from a model/buyer wire payload. Callbacks are trusted bounded
host checks; they must fail closed and must not initiate a send. New v3 stores
are deliberately incompatible with lab v2. No migration is performed.
"""
import inspect
import json
import math
import re
from hashlib import sha256

from .admission import AdmissionRequest, verify_evidence
from .autonomy import CurrentBasis, PolicyDecision, _bounded_json, evaluate_autonomy
from .contracts import Context, bounded_text, canonical_json
from .delivery import command_body_digest
from .runtime import Runtime, _SCHEMA_OBJECTS
from .api_acceptance import (ApiAcceptedReceipt, MAX_PROOF_BYTES, PROOF_EVENT,
                             PROOF_LEVEL, PROOF_SOURCE, validate_proof)


_SCOPE = ("tenant_id", "shop_id", "channel", "account_id")
_BASIS = ("package_sha256", "knowledge_sha256", "model_sha256", "policy_sha256")
_HASH = re.compile(r"[a-f0-9]{64}")
_AUTO_ACTIONS = frozenset({"auto_reply", "auto_clarify"})
_AUTONOMOUS_SCHEMA = dict(_SCHEMA_OBJECTS)
_AUTONOMOUS_SCHEMA.update({
    "admission_records": ("table", "admission_records", """CREATE TABLE admission_records (
      admission_id TEXT PRIMARY KEY, request_json TEXT NOT NULL,
      request_sha256 TEXT NOT NULL, ai_reference TEXT NOT NULL,
      human_reference TEXT NOT NULL, ai_record_sha256 TEXT NOT NULL,
      human_record_sha256 TEXT NOT NULL, created_at REAL NOT NULL
    )"""),
    "admission_revocations": ("table", "admission_revocations", """CREATE TABLE admission_revocations (
      admission_id TEXT PRIMARY KEY REFERENCES admission_records(admission_id),
      created_at REAL NOT NULL
    )"""),
    "policy_authorizations": ("table", "policy_authorizations", """CREATE TABLE policy_authorizations (
      command_id TEXT PRIMARY KEY REFERENCES commands(id),
      admission_id TEXT NOT NULL REFERENCES admission_records(admission_id),
      request_sha256 TEXT NOT NULL, action TEXT NOT NULL,
      history_sha256 TEXT NOT NULL, body_sha256 TEXT NOT NULL,
      context_key TEXT NOT NULL REFERENCES sessions(key), revision INTEGER NOT NULL,
      in_reply_to TEXT NOT NULL, created_at REAL NOT NULL
    )"""),
    "generation_claims": ("table", "generation_claims", """CREATE TABLE generation_claims (
      session_key TEXT NOT NULL REFERENCES sessions(key), in_reply_to TEXT NOT NULL,
      revision INTEGER NOT NULL, history_sha256 TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('active','finished')),
      PRIMARY KEY(session_key,in_reply_to,revision)
    )"""),
    "generation_claims_intent_immutable": ("trigger", "generation_claims", """CREATE TRIGGER generation_claims_intent_immutable
      BEFORE UPDATE OF session_key,in_reply_to,revision,history_sha256 ON generation_claims
      WHEN NEW.session_key IS NOT OLD.session_key OR NEW.in_reply_to IS NOT OLD.in_reply_to
        OR NEW.revision IS NOT OLD.revision OR NEW.history_sha256 IS NOT OLD.history_sha256 BEGIN
        SELECT RAISE(ABORT, 'Generation intent is immutable');
      END"""),
    "generation_claims_state_monotonic": ("trigger", "generation_claims", """CREATE TRIGGER generation_claims_state_monotonic
      BEFORE UPDATE OF state ON generation_claims
      WHEN OLD.state != 'active' OR NEW.state != 'finished' BEGIN
        SELECT RAISE(ABORT, 'Generation completion is irreversible');
      END"""),
    "generation_claims_no_delete": ("trigger", "generation_claims", """CREATE TRIGGER generation_claims_no_delete
      BEFORE DELETE ON generation_claims BEGIN
        SELECT RAISE(ABORT, 'Generation claims are immutable');
      END"""),
})
for _table in ("admission_records", "admission_revocations", "policy_authorizations"):
    for _verb in ("UPDATE", "DELETE"):
        _name = _table + "_no_" + _verb.lower()
        _AUTONOMOUS_SCHEMA[_name] = (
            "trigger", _table,
            f"CREATE TRIGGER {_name} BEFORE {_verb} ON {_table} BEGIN "
            "SELECT RAISE(ABORT, 'Autonomous authorization facts are immutable'); END",
        )


def _identifier(value):
    try:
        bounded_text(value, "identifier")
    except (ValueError, TypeError):
        raise ValueError("autonomy_input_invalid") from None
    if type(value) is not str or value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError("autonomy_input_invalid")


def _history_hash(value):
    if type(value) is not str or not _HASH.fullmatch(value):
        raise ValueError("autonomy_history_invalid")


class AutonomousRuntime(Runtime):
    """One owner, one existing send state machine, immutable host grants.

    ``basis_reader(context)`` returns CurrentBasis from the live trusted host.
    ``before_send(command)`` rechecks Stop, account ownership and permissions;
    only an exact None result succeeds. Exceptions and False both fail closed.
    Evidence readers are required even for reopened stores: previously passed
    evidence is re-read at every admission and irreversible-send boundary.
    """
    _schema_version = 3
    _schema_objects = _AUTONOMOUS_SCHEMA

    def __init__(self, db_path, *, evidence_reader, basis_reader, before_send,
                 clock=None, allowed_versions=("mock-1",), api_acceptance_reader=None):
        if (not all(callable(callback) for callback in (evidence_reader, basis_reader, before_send))
                or clock is not None and not callable(clock)
                or api_acceptance_reader is not None and not callable(api_acceptance_reader)):
            raise ValueError("autonomy_host_callbacks_required")
        self._evidence_reader = evidence_reader
        self._basis_reader = basis_reader
        self._before_send = before_send
        # Optional installed-adapter observation source, never an RPC payload or
        # a model-provided trust flag. Old DeliveryReceipt behavior is unchanged.
        self._api_acceptance_reader = api_acceptance_reader
        super().__init__(db_path, clock=clock, allowed_versions=allowed_versions)

    def _assert_open(self):
        if self._closed:
            raise RuntimeError("Runtime closed")

    def _now(self):
        try:
            value = self.clock()
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 253402300799:
                raise ValueError
            return int(value)
        except Exception:
            raise ValueError("autonomy_clock_unavailable") from None

    def _basis(self, context):
        try:
            basis = self._basis_reader(context)
            if type(basis) is not CurrentBasis:
                raise ValueError
            return basis
        except Exception:
            raise ValueError("autonomy_basis_unavailable") from None

    def _evidence(self, request, ai_reference, human_reference):
        try:
            return verify_evidence(request, ai_reference, human_reference, self._evidence_reader)
        except Exception:
            raise ValueError("autonomy_evidence_invalid") from None

    def issue_admission(self, request, *, ai_reference, human_reference):
        """Persist an exact independently accepted request; never widen/revive it."""
        if type(request) is not AdmissionRequest:
            raise ValueError("autonomy_admission_request_required")
        with self._transaction():
            previous = self.db.execute(
                "SELECT * FROM admission_records WHERE admission_id=?", (request.admission_id,)
            ).fetchone()
            if self.db.execute("SELECT 1 FROM admission_revocations WHERE admission_id=?",
                               (request.admission_id,)).fetchone():
                raise ValueError("autonomy_admission_revoked")
            ai, human = self._evidence(request, ai_reference, human_reference)
            values = (canonical_json(request.as_dict()).decode("utf-8"), request.digest,
                      ai.reference, human.reference, ai.record_sha256, human.record_sha256)
            if previous is not None:
                fields = ("request_json", "request_sha256", "ai_reference", "human_reference",
                          "ai_record_sha256", "human_record_sha256")
                if tuple(previous[field] for field in fields) != values:
                    raise ValueError("autonomy_admission_conflict")
                return request.snapshot()
            self.db.execute("INSERT INTO admission_records VALUES(?,?,?,?,?,?,?,?)",
                            (request.admission_id, *values, self._now()))
            self._audit("admission_issued", reason="accepted_evidence_bound")
            return request.snapshot()

    def revoke_admission(self, admission_id):
        """Append an irreversible local revocation; a new scope needs a new ID."""
        _identifier(admission_id)
        with self._transaction():
            if not self.db.execute("SELECT 1 FROM admission_records WHERE admission_id=?",
                                   (admission_id,)).fetchone():
                raise ValueError("autonomy_admission_missing")
            cursor = self.db.execute("INSERT OR IGNORE INTO admission_revocations VALUES(?,?)",
                                     (admission_id, self._now()))
            if cursor.rowcount:
                self._audit("admission_revoked", reason="host_revoked")

    def _live_admission(self, admission_id, context):
        """Read-only helper, safe inside an already-owned transaction/lock."""
        _identifier(admission_id)
        if type(context) is not Context:
            raise ValueError("autonomy_input_invalid")
        self._assert_open()
        row = self.db.execute("SELECT * FROM admission_records WHERE admission_id=?",
                              (admission_id,)).fetchone()
        if row is None:
            raise ValueError("autonomy_admission_missing")
        if self.db.execute("SELECT 1 FROM admission_revocations WHERE admission_id=?",
                           (admission_id,)).fetchone():
            raise ValueError("autonomy_admission_revoked")
        try:
            request = AdmissionRequest.from_dict(json.loads(row["request_json"]))
            if (request.admission_id != admission_id or request.digest != row["request_sha256"]
                    or canonical_json(request.as_dict()).decode("utf-8") != row["request_json"]):
                raise ValueError
        except Exception:
            raise ValueError("autonomy_admission_corrupt") from None
        if any(getattr(request, name) != getattr(context, name) for name in _SCOPE):
            raise ValueError("autonomy_admission_scope_mismatch")
        now = self._now()
        if not request.valid_from <= now < request.valid_until:
            raise ValueError("autonomy_admission_not_current")
        ai, human = self._evidence(request, row["ai_reference"], row["human_reference"])
        if ai.record_sha256 != row["ai_record_sha256"] or human.record_sha256 != row["human_record_sha256"]:
            raise ValueError("autonomy_evidence_changed")
        # Readers are trusted, but may reenter the owner (e.g. revocation or
        # takeover). Evaluate current basis after evidence, then read local
        # revocation/time again after callbacks have returned.
        basis = self._basis(context)
        if any(getattr(request, name) != getattr(basis, name) for name in _BASIS):
            raise ValueError("autonomy_basis_changed")
        now = self._now()
        if self.db.execute("SELECT 1 FROM admission_revocations WHERE admission_id=?",
                           (admission_id,)).fetchone():
            raise ValueError("autonomy_admission_revoked")
        if not request.valid_from <= now < request.valid_until:
            raise ValueError("autonomy_admission_not_current")
        return request, basis, now

    def admission(self, admission_id, context):
        with self._lock:
            request, _, _ = self._live_admission(admission_id, context)
            return request.snapshot()

    def preflight(self, admission_id, context):
        with self._lock:
            request, _, _ = self._live_admission(admission_id, context)
            count = self.db.execute("SELECT COUNT(*) FROM policy_authorizations WHERE admission_id=?",
                                    (admission_id,)).fetchone()[0]
            if count >= request.max_auto_messages:
                raise ValueError("autonomy_message_limit")
            return request.snapshot()

    def _generation_session(self, context, in_reply_to, revision, history_sha256):
        if type(context) is not Context or type(revision) is not int or revision < 1:
            raise ValueError("autonomy_generation_invalid")
        _identifier(in_reply_to)
        _history_hash(history_sha256)
        self._assert_open()
        session = self.db.execute("SELECT * FROM sessions WHERE key=?", (context.key,)).fetchone()
        if (session is None or session["mode"] != "assistant" or session["revision"] != revision
                or session["last_message_id"] != in_reply_to):
            raise ValueError("autonomy_generation_stale")
        if self.db.execute("SELECT 1 FROM commands WHERE session_key=? AND status IN ('dispatching','unknown')",
                           (context.key,)).fetchone():
            raise ValueError("autonomy_delivery_unresolved")

    def claim_generation(self, context, in_reply_to, revision, history_sha256):
        """Persist a single attempt before the model; failure never releases it."""
        with self._transaction():
            self._generation_session(context, in_reply_to, revision, history_sha256)
            row = self.db.execute(
                "SELECT history_sha256 FROM generation_claims WHERE session_key=? AND in_reply_to=? AND revision=?",
                (context.key, in_reply_to, revision)).fetchone()
            if row is not None:
                if row["history_sha256"] != history_sha256:
                    raise ValueError("autonomy_generation_history_mismatch")
                return False
            if self.db.execute("SELECT 1 FROM generation_claims WHERE session_key=? AND state='active'",
                               (context.key,)).fetchone():
                raise ValueError("autonomy_generation_inflight")
            self.db.execute("INSERT INTO generation_claims VALUES(?,?,?,?,'active')",
                            (context.key, in_reply_to, revision, history_sha256))
            self._audit("generation_claimed", context.key, reason="single_host_attempt")
            return True

    def _generation_claim(self, context, in_reply_to, revision, history_sha256):
        row = self.db.execute(
            "SELECT history_sha256,state FROM generation_claims WHERE session_key=? AND in_reply_to=? AND revision=?",
            (context.key, in_reply_to, revision)).fetchone()
        if row is None or row["history_sha256"] != history_sha256:
            raise ValueError("autonomy_generation_claim_mismatch")
        return row

    def assert_generation_current(self, context, in_reply_to, revision, history_sha256):
        with self._lock:
            self._generation_session(context, in_reply_to, revision, history_sha256)
            self._generation_claim(context, in_reply_to, revision, history_sha256)

    def mark_generation_finished(self, context, in_reply_to, revision, history_sha256):
        """Host attests the original call ended; unknown/cancelled calls stay active.

        A completed call can be settled after takeover or a newer inbound turn.
        This does not make its old result current or permit another attempt for
        the original message. The host must not call this on timeout/uncertainty.
        """
        if type(context) is not Context or type(revision) is not int or revision < 1:
            raise ValueError("autonomy_generation_invalid")
        _identifier(in_reply_to)
        _history_hash(history_sha256)
        with self._transaction():
            self._generation_claim(context, in_reply_to, revision, history_sha256)
            row = self.db.execute(
                "SELECT state FROM generation_claims WHERE session_key=? AND in_reply_to=? AND revision=?",
                (context.key, in_reply_to, revision)).fetchone()
            if row["state"] == "finished":
                return False
            self.db.execute(
                "UPDATE generation_claims SET state='finished' WHERE session_key=? AND in_reply_to=? AND revision=?",
                (context.key, in_reply_to, revision))
            self._audit("generation_finished", context.key, reason="host_confirmed_call_finished")
            return True

    def clarification_turns(self, context):
        if type(context) is not Context:
            raise ValueError("autonomy_input_invalid")
        with self._lock:
            self._assert_open()
            return self.db.execute(
                "SELECT COUNT(*) FROM policy_authorizations WHERE context_key=? AND action='auto_clarify'",
                (context.key,)).fetchone()[0]

    def authorize_candidate(self, command_id, *, admission_id, candidate, expected_history_hash):
        """Re-evaluate the host candidate and approve only its exact stored body."""
        _identifier(command_id)
        _history_hash(expected_history_hash)
        with self._transaction():
            command = self._command(command_id)
            if command["status"] != "draft" or not self._current(command):
                raise ValueError("autonomy_command_not_current_draft")
            context = command["context"]
            self._generation_session(context, command["in_reply_to"], command["revision"], expected_history_hash)
            claim = self._generation_claim(context, command["in_reply_to"], command["revision"], expected_history_hash)
            request, basis, now = self._live_admission(admission_id, context)
            try:
                _bounded_json(candidate)
                candidate = json.loads(canonical_json(candidate))
            except (ValueError, TypeError, RecursionError, OverflowError, UnicodeError):
                candidate = None
            decision = evaluate_autonomy(
                admission=request.snapshot(), basis=basis, context=context, candidate=candidate,
                expected_history_hash=expected_history_hash, now=now,
                clarification_turns=min(self.clarification_turns(context), 1000))
            if decision.action not in _AUTO_ACTIONS:
                self._audit("policy_not_authorized", context.key, command_id, decision.reason_codes[0])
                return decision
            if claim["state"] != "finished":
                raise ValueError("autonomy_generation_unfinished")
            if decision.reply_sha256 != command_body_digest(command):
                raise ValueError("autonomy_command_body_mismatch")
            latest = candidate["handoff"]["conversation"][-1]
            inbound = self.db.execute("SELECT text FROM inbox WHERE session_key=? AND message_id=?",
                                      (context.key, command["in_reply_to"])).fetchone()
            if (latest["role"] != "customer" or latest["id"] != command["in_reply_to"]
                    or inbound is None or latest["text"] != inbound["text"]):
                raise ValueError("autonomy_candidate_inbound_mismatch")
            # A reader callback may have changed ownership while policy facts
            # were read. The immutable candidate must still target the live turn.
            if not self._current(command) or self._command(command_id)["status"] != "draft":
                raise ValueError("autonomy_command_not_current_draft")
            count = self.db.execute("SELECT COUNT(*) FROM policy_authorizations WHERE admission_id=?",
                                    (admission_id,)).fetchone()[0]
            if count >= request.max_auto_messages:
                decision = PolicyDecision("hold", ("autonomy_message_limit",), decision.reply_sha256, admission_id)
                self._audit("policy_not_authorized", context.key, command_id, decision.reason_codes[0])
                return decision
            self.db.execute("INSERT INTO policy_authorizations VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (command_id, admission_id, request.digest, decision.action,
                             expected_history_hash, decision.reply_sha256, context.key,
                             command["revision"], command["in_reply_to"], now))
            self._update(command_id, "approved", "policy_admission_authorized")
            return decision

    def approve(self, command_id):
        raise ValueError("autonomy_policy_authorization_required")

    def _assert_authorization(self, command):
        record = self.db.execute("SELECT * FROM policy_authorizations WHERE command_id=?",
                                 (command["command_id"],)).fetchone()
        if record is None:
            raise ValueError("autonomy_policy_authorization_missing")
        request, _, _ = self._live_admission(record["admission_id"], command["context"])
        if (record["request_sha256"] != request.digest or record["action"] not in _AUTO_ACTIONS
                or record["action"] not in request.authorized_reply_actions
                or record["body_sha256"] != command_body_digest(command)
                or record["context_key"] != command["context"].key
                or record["context_key"] != command["session_key"]
                or record["revision"] != command["revision"]
                or record["in_reply_to"] != command["in_reply_to"]):
            raise ValueError("autonomy_policy_authorization_mismatch")
        claim = self._generation_claim(command["context"], command["in_reply_to"], command["revision"], record["history_sha256"])
        if claim["state"] != "finished":
            raise ValueError("autonomy_generation_unfinished")
        count = self.db.execute("SELECT COUNT(*) FROM policy_authorizations WHERE admission_id=?",
                                (request.admission_id,)).fetchone()[0]
        if count > request.max_auto_messages:
            raise ValueError("autonomy_message_limit")

    def _host_send_check(self, command):
        try:
            checked = self._before_send(dict(command))
            if inspect.iscoroutine(checked):
                # The synchronous API cannot safely run an asynchronous host
                # decision. Close the unstarted coroutine and fail closed.
                checked.close()
            if checked is not None:
                raise ValueError
        except Exception:
            raise ValueError("autonomy_host_send_denied") from None

    def dispatch(self, command_id, adapter):
        """Pre-send invalidations block safely; the base owns all send outcomes."""
        with self._operation():
            with self._lock:
                command = self._command(command_id)
                if command["status"] != "approved":
                    return command
                try:
                    self._assert_authorization(command)
                    self._host_send_check(command)
                    self._assert_authorization(self._command(command_id))
                except ValueError as error:
                    # Only stable locally generated codes reach the audit log.
                    reason = str(error)
                    with self._transaction():
                        if self._command(command_id)["status"] == "approved":
                            self._update(command_id, "blocked", reason)
                        return self._command(command_id)
            return super()._dispatch(command_id, adapter)

    def validate_execution_fence(self, fence, outgoing):
        with self._lock:
            super().validate_execution_fence(fence, outgoing)
            command = self._command(fence.command_id)
            self._assert_authorization(command)
            self._host_send_check(command)
            # Reentrant trusted host checks may revoke/take over while running.
            super().validate_execution_fence(fence, outgoing)
            self._assert_authorization(self._command(fence.command_id))
            super().validate_execution_fence(fence, outgoing)
            return fence

    async def _host_send_check_async(self, command):
        try:
            checked = self._before_send(dict(command))
            if inspect.isawaitable(checked):
                checked = await checked
            if checked is not None:
                raise ValueError
        except Exception:
            raise ValueError("autonomy_host_send_denied") from None

    async def validate_execution_fence_async(self, fence, outgoing):
        """Final async connector guard, without holding a lock across await.

        A connector MUST call this after its last preparation await and directly
        before the irreversible platform send. Its transport remains responsible
        for atomic recipient/ownership checks and platform idempotency. Neither
        this guard nor a snapshot can remove cross-platform takeover races.
        """
        with self._lock:
            super().validate_execution_fence(fence, outgoing)
            command = self._command(fence.command_id)
            self._assert_authorization(command)
            super().validate_execution_fence(fence, outgoing)
        await self._host_send_check_async(command)
        with self._lock:
            super().validate_execution_fence(fence, outgoing)
            self._assert_authorization(self._command(fence.command_id))
            # Evidence/basis readers can reenter receive/takeover/revoke.
            super().validate_execution_fence(fence, outgoing)
        return fence

    async def dispatch_async(self, command_id, adapter):
        """The original outbox transitions with awaited channel operations.

        Real transports implement async snapshot/send/lookup; immediate return
        values remain supported for existing nonblocking local adapters. Blocking
        SDK calls must not be passed here as synchronous methods. No thread,
        background task, retry or second outbox is created by this method.
        """
        with self._operation():
            with self._lock:
                command = self._command(command_id)
                if command["status"] != "approved":
                    return command
            try:
                with self._lock:
                    self._assert_authorization(command)
                await self._host_send_check_async(command)
                with self._lock:
                    command = self._command(command_id)
                    if command["status"] != "approved":
                        return command
                    self._assert_authorization(command)
            except ValueError as error:
                with self._transaction():
                    if self._command(command_id)["status"] == "approved":
                        self._update(command_id, "blocked", str(error))
                    return self._command(command_id)

            with self._transaction():
                command = self._command(command_id)
                if command["status"] != "approved":
                    return command
                if not self._current(command):
                    self._update(command_id, "cancelled", "stale_before_snapshot")
                    return self._command(command_id)
            try:
                snapshot = adapter.snapshot(command["context"])
                if inspect.isawaitable(snapshot):
                    snapshot = await snapshot
                observed = snapshot.get("context")
                if isinstance(observed, dict):
                    observed = Context(**observed)
                reason = ("recipient_mismatch" if observed != command["context"] else
                          "unsupported_client_version" if snapshot.get("version") not in self.allowed_versions else
                          "channel_not_ready" if snapshot.get("ready") is not True else
                          "manual_operator_active" if snapshot.get("manual_active") is not False else "")
            except Exception:
                reason = "snapshot_unavailable"
            with self._transaction():
                command = self._command(command_id)
                if command["status"] != "approved":
                    return command
                if not self._current(command):
                    self._update(command_id, "cancelled", "stale_after_snapshot")
                    return self._command(command_id)
                if reason:
                    self._update(command_id, "blocked", reason)
                    return self._command(command_id)
                self._update(command_id, "dispatching", "write_ahead_before_adapter")
                command = self._command(command_id)
                fence = self._fence_for_command(command)
                command["execution_fence"] = fence
            try:
                # Recheck authority/basis/Stop/session after snapshot's await.
                await self.validate_execution_fence_async(fence, command)
                result = adapter.send(command)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:
                result = None
            except BaseException:
                # Cancellation cannot prove the external operation did not send.
                # Persist uncertainty immediately, then propagate cancellation.
                with self._transaction():
                    if self._command(command_id)["status"] == "dispatching":
                        self._update(command_id, "unknown", "send_interrupted")
                raise
            with self._transaction():
                if self._command(command_id)["status"] == "dispatching":
                    # A takeover/revocation during send cannot recall a message.
                    # Record actual bound evidence even if authority is now gone;
                    # do not turn a confirmed past effect into permission to send.
                    self._record_result(command_id, result)
                return self._command(command_id)

    def _record_result(self, command_id, result):
        if type(result) is not ApiAcceptedReceipt:
            return super()._record_result(command_id, result)
        command = self._command(command_id)
        if command["status"] not in {"dispatching", "unknown"}:
            return
        try:
            if self._api_acceptance_reader is None:
                raise ValueError("api_acceptance_reader_required")
            proof = self._api_acceptance_reader(result)
            if inspect.isawaitable(proof):
                if inspect.iscoroutine(proof):
                    proof.close()
                raise ValueError("api_acceptance_reader_must_be_local")
            proof, evidence = validate_proof(command, result, proof)
        except Exception:
            if self._command(command_id)["status"] in {"dispatching", "unknown"}:
                self._update(command_id, "unknown", "api_acceptance_unverified")
            return
        # A trusted reader may reenter; it cannot downgrade another observation
        # or grant authority. This records only the original, already sent intent.
        if self._command(command_id)["status"] not in {"dispatching", "unknown"}:
            return
        evidence_id = sha256(canonical_json(evidence)).hexdigest()
        self._audit(PROOF_EVENT, command["context"].key, command_id,
                    canonical_json(proof).decode("utf-8"))
        self.db.execute(
            "INSERT OR IGNORE INTO delivery_evidence "
            "(evidence_id,command_id,context_json,revision,in_reply_to,body_sha256,level,source,"
            "receipt_id,not_sent_proof_id,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (evidence_id, command_id, canonical_json(evidence["context"]).decode("utf-8"),
             evidence["revision"], evidence["in_reply_to"], evidence["body_sha256"],
             PROOF_LEVEL, PROOF_SOURCE, evidence["receipt_id"], None, self.clock()),
        )
        # API acceptance is a separate terminal send state, not confirmed,
        # delivered or read. Its immutable proof is in evidence, not receipt_id.
        self._update(command_id, "api_accepted", "authenticated_api_acceptance")

    def read_api_acceptance(self, command_id):
        """Return the original retained API proof, or None; never send or retry.

        Audit is not immutable, so its exact proof must still match the digest in
        immutable evidence and the original intent. No current grant is needed
        to observe historical API acceptance; this method grants no send rights.
        """
        with self._lock:
            self._assert_open()
            command = self._command(command_id)
            row = self.db.execute(
                "SELECT * FROM delivery_evidence WHERE command_id=? AND level=? AND source=? LIMIT 2",
                (command_id, PROOF_LEVEL, PROOF_SOURCE)).fetchall()
            if len(row) != 1:
                return None
            stored = row[0]
            records = self.db.execute(
                "SELECT reason FROM audit_events WHERE command_id=? AND session_key=? AND event=? "
                "AND length(reason)<=? ORDER BY sequence DESC LIMIT 2",
                (command_id, command["context"].key, PROOF_EVENT, MAX_PROOF_BYTES)).fetchall()
            for record in records:
                try:
                    raw = record["reason"]
                    if len(raw.encode("utf-8")) > MAX_PROOF_BYTES:
                        continue
                    proof = json.loads(raw)
                    receipt = ApiAcceptedReceipt(command["context"], command_id, stored["receipt_id"])
                    proof, evidence = validate_proof(command, receipt, proof)
                    if (stored["evidence_id"] != sha256(canonical_json(evidence)).hexdigest()
                            or stored["context_json"] != canonical_json(evidence["context"]).decode("utf-8")
                            or any(stored[key] != evidence[key] for key in (
                                "command_id", "revision", "in_reply_to", "body_sha256", "level",
                                "source", "receipt_id", "not_sent_proof_id"))):
                        continue
                    return proof
                except (ValueError, TypeError, KeyError, RecursionError):
                    continue
            return None

    async def reconcile_async(self, command_id, adapter):
        """Await exact-original receipt lookup, never dispatch or resend.

        Revoked/expired grants and human-owned sessions may still reconcile a
        past uncertain effect. Lookup is not send authorization and must be a
        read-only, authenticated connector operation for the original account.
        """
        with self._operation():
            with self._lock:
                command = self._command(command_id)
                if command["status"] != "unknown":
                    return command
            try:
                result = adapter.lookup(command)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:
                result = None
            # CancelledError propagates; the durable outcome stays unknown.
            with self._transaction():
                if self._command(command_id)["status"] == "unknown":
                    self._record_result(command_id, result)
                return self._command(command_id)
