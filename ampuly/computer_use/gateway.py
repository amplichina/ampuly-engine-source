"""The one Computer Use Gateway (4.3 X01): every action reaches an executor through here or not at all.

First slice: the refusal set, before any executor is wired. An action is admitted
only when all of these hold, each refused by name when it does not:

- it is a contract (`parse_action`), so unknown kinds and upstream function names
  never get past the door (G01, G06, C25);
- it binds to the scope and to the hash of the observation it was decided on, and
  that observation is the latest one for the session (G05, C05);
- a coordinate action's geometry is the observed geometry and has not expired (C30);
- the control lease names the agent as the current holder -- while a person
  operates, or after a hand-back that is not yet verified, nothing is admitted
  (G04, C06, C07);
- egress stays inside the session's allowed hosts (C02);
- an external side effect carries a verification plan with an independent source
  (§5.4, C11), and its first attempt binds to an Effect claim so a crash after
  dispatch is `outcome_unknown`, never "did not happen" (G09, C28);
- a new attempt of the same action is admitted only when the previous receipt
  says `failed_before_effect` and the contract is retryable; after
  `failed_after_effect` / `outcome_unknown` / a timeout the facts come first (G03).

And on the way back: an executor's receipt may say `succeeded` only with
independent verification evidence; its own `finished` / `verified` /
`production` labels are input, not outcome (C31, G06).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from ampuly.computer_use.contracts import (
    INDEPENDENT_SOURCES, ActionClass, ActionReceipt, ComputerAction, ContractRefused, Observation, Outcome,
    parse_action,
)


class LeaseState(str, Enum):
    """§5.5 control-lease states. Only one of them lets an agent act."""

    CREATED = "created"
    AGENT_OPERATING = "agent_operating"
    HANDOFF_REQUESTED = "handoff_requested"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN_OPERATING = "human_operating"
    HUMAN_RETURNED = "human_returned"
    IDENTITY_CHECKING = "identity_checking"
    IDENTITY_VERIFIED = "identity_verified"
    REVOKED = "revoked"
    EXPIRED = "expired"
    CLOSE_PENDING = "close_pending"
    CLOSE_FAILED = "close_failed"
    CLOSED = "closed"


AGENT_MAY_ACT = frozenset({LeaseState.AGENT_OPERATING, LeaseState.IDENTITY_VERIFIED})


@dataclass
class ControlLease:
    """Server-side control of one session: who holds it, which scope, until when."""

    lease_id: str
    session_id: str
    scope_digest: str
    state: LeaseState = LeaseState.CREATED
    expires_at: float = 0.0
    allowed_hosts: tuple[str, ...] = ()

    def agent_may_act(self, now: float) -> bool:
        return self.state in AGENT_MAY_ACT and (self.expires_at == 0.0 or now < self.expires_at)


class GatewayRefused(PermissionError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class Admission:
    """What an executor is handed: the action, the observation it binds to, and (for an
    external effect) the Effect claim it must carry. Never business data or credentials."""

    action: ComputerAction
    observation_hash: str
    effect_claim: str = ""
    admitted_at: float = 0.0


@dataclass
class _Session:
    lease: ControlLease
    latest_observation: Observation | None = None
    receipts: dict[str, list[ActionReceipt]] = field(default_factory=dict)
    admitted: dict[str, Admission] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    # Attempt ids a previous process admitted (from the journal); never reusable.
    spent_attempts: set[str] = field(default_factory=set)
    # C12: a cancelled turn admits nothing more until the next observation.
    cancelled_until_observed: bool = False
    # A write whose effect is not known blocks later writes until a
    # model-facing observation records fresh facts.  This is rebuilt from the
    # durable journal after a restart.
    unresolved: dict[str, str] = field(default_factory=dict)
    journal_unreadable: bool = False


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    """A delegation authorizes hosts; without one no host is authorized."""
    if not allowed:
        return False
    host = host.lower().strip()
    return any(host == a or host.endswith("." + a) for a in allowed)


class GatewayJournal:
    """Append-only record of what the Gateway admitted, refused and receipted (C24, C14).

    One JSON object per line, never a payload, a page or a credential: the
    same minimal events the in-memory log holds. Read back only for two
    things a new process must not forget -- which attempt ids were used and
    what each action's last receipt said -- so a retry after outcome_unknown
    is refused across a restart exactly as within one (G03).
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(self, session_id: str, event: dict[str, Any]) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"session_id": session_id, **event}, ensure_ascii=False, default=str) + "\n")
                # An admission must be on disk before the action is dispatched. A bulk P4 item row must be on disk
                # because being kept IS what it is for: a row lost to a hard kill leaves that item with no row,
                # and an item with no row is counted 结果不明 and handed to a person -- never counted as passed.
                if event.get("type") in {"admitted", "item_result"}:
                    handle.flush()
                    os.fsync(handle.fileno())
            return True
        except OSError:
            return False

    def events(self, session_id: str) -> list[dict[str, Any]]:
        """Read this session's durable audit events, ignoring torn/corrupt lines.

        This is an audit projection only.  Recovery continues to use
        :meth:`replay`, whose stricter interpretation owns execution state.
        """
        if not self.path.is_file():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("session_id") != session_id:
                continue
            events.append({key: value for key, value in event.items() if key != "session_id"})
        return events

    def replay(self, session_id: str) -> tuple[set[str], dict[str, ActionReceipt], dict[str, str], bool]:
        """Restore spent ids, last receipts and unresolved writes for one session."""
        attempts: set[str] = set()
        last: dict[str, ActionReceipt] = {}
        unresolved: dict[str, str] = {}
        open_attempts: dict[str, tuple[str, str]] = {}
        if not self.path.is_file():
            return attempts, last, unresolved, False
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return attempts, last, unresolved, True
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("session_id") != session_id:
                continue
            kind = event.get("type")
            if kind == "admitted" and event.get("attempt_id"):
                attempt_id = str(event["attempt_id"])
                attempts.add(attempt_id)
                open_attempts[attempt_id] = (str(event.get("action_id", "") or ""), str(event.get("risk", "") or ""))
            elif kind == "receipt" and event.get("action_id"):
                try:
                    outcome = Outcome(str(event.get("outcome", "")))
                except ValueError:
                    continue
                action_id = str(event["action_id"])
                attempt_id = str(event.get("attempt_id", "") or "")
                reason = str(event.get("reason", "") or "")[:200]
                last[action_id] = ActionReceipt(action_id, attempt_id, outcome, reason=reason)
                _, admitted_risk = open_attempts.pop(attempt_id, (action_id, ""))
                risk = str(event.get("risk", "") or admitted_risk)
                if self._locks_writes(outcome, reason, risk):
                    unresolved[action_id] = outcome.value
            elif kind == "facts_observed":
                resolved = {str(action_id) for action_id in list(event.get("resolved", []) or [])}
                for action_id in resolved:
                    unresolved.pop(action_id, None)
                open_attempts = {
                    attempt_id: identity
                    for attempt_id, identity in open_attempts.items()
                    if identity[0] not in resolved
                }
        # A durable admission without a durable receipt may have reached the
        # executor.  Conservatively restore it as unknown, never as absent.
        for attempt_id, (action_id, risk) in open_attempts.items():
            if not action_id:
                continue
            receipt = ActionReceipt(action_id, attempt_id, Outcome.OUTCOME_UNKNOWN, reason="no_receipt_before_restart")
            last[action_id] = receipt
            if risk in {ActionClass.REVERSIBLE_WRITE.value, ActionClass.HOST.value}:
                unresolved[action_id] = receipt.outcome.value
        return attempts, last, unresolved, False

    @staticmethod
    def _locks_writes(outcome: Outcome, reason: str, risk: str) -> bool:
        if risk not in {ActionClass.REVERSIBLE_WRITE.value, ActionClass.HOST.value}:
            return False
        return (
            outcome in {Outcome.CANCELLED_UNKNOWN, Outcome.FAILED_AFTER_EFFECT}
            or (outcome is Outcome.OUTCOME_UNKNOWN and reason != "input_accepted_unverified")
        )


class ComputerUseGateway:
    """One instance per process. Executors are registered later; today only the door exists."""

    def __init__(self, *, effect_claimer: Callable[[ComputerAction], str] | None = None, clock: Callable[[], float] = time.time,
                 journal: GatewayJournal | None = None) -> None:
        self._sessions: dict[str, _Session] = {}
        self._claim = effect_claimer
        self._clock = clock
        self._journal = journal

    # ── session and lease ──────────────────────────────────────────────

    def open_session(self, lease: ControlLease) -> None:
        if lease.session_id in self._sessions:
            raise GatewayRefused("session_exists", lease.session_id)
        session = _Session(lease=lease)
        if self._journal is not None:
            # What an earlier process did with this session is still true.
            attempts, last, unresolved, unreadable = self._journal.replay(lease.session_id)
            session.spent_attempts = attempts
            session.unresolved = unresolved
            session.journal_unreadable = unreadable
            for action_id, receipt in last.items():
                session.receipts[action_id] = [receipt]
        self._sessions[lease.session_id] = session
        self._event(session, lease.session_id, {"type": "session", "lease_id": lease.lease_id, "state": lease.state.value,
                                                 "restored_receipts": len(session.receipts), "at": self._clock()})

    def lease(self, session_id: str) -> ControlLease:
        return self._require(session_id).lease

    def set_lease_state(self, session_id: str, state: LeaseState, *, note: str = "") -> ControlLease:
        session = self._require(session_id)
        session.lease.state = state
        self._event(session, session_id, {"type": "lease", "state": state.value, "note": note[:200], "at": self._clock()})
        return session.lease

    def observe(self, observation: Observation) -> str:
        """Record the latest observation; everything decided on an older one is stale."""
        session = self._require(observation.session_id)
        previous = session.latest_observation
        if previous is not None and observation.sequence <= previous.sequence:
            raise GatewayRefused("observation_out_of_order", f"{observation.sequence} <= {previous.sequence}")
        if observation.scope_digest != session.lease.scope_digest:
            raise GatewayRefused("observation_scope_mismatch")
        session.latest_observation = observation
        session.cancelled_until_observed = False
        digest = observation.digest()
        self._event(session, observation.session_id, {"type": "observation", "sequence": observation.sequence, "hash": digest, "at": self._clock()})
        return digest

    def events(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._require(session_id).events)

    def facts_observed(self, session_id: str, attempt_id: str) -> bool:
        """Clear unresolved writes only after a successful model-facing observation."""

        session = self._require(session_id)
        admission = session.admitted.get(str(attempt_id or ""))
        if admission is None or admission.action.kind != "observe":
            raise GatewayRefused("facts_not_observed", "only an admitted observation can resolve unknown writes")
        receipts = session.receipts.get(admission.action.action_id, [])
        if not any(r.attempt_id == attempt_id and r.outcome is Outcome.SUCCEEDED for r in receipts):
            raise GatewayRefused("facts_not_observed", "the observation has no successful receipt")
        resolved = sorted(session.unresolved)
        if not resolved:
            return True
        event = {"type": "facts_observed", "attempt_id": attempt_id, "resolved": resolved, "at": self._clock()}
        if self._journal is not None and not self._journal.append(session_id, event):
            session.events.append({**event, "type": "journal_degraded"})
            return False
        session.events.append(event)
        session.unresolved.clear()
        return True

    def cancel_in_flight(self, session_id: str, *, reason: str = "turn_cancelled") -> list[ActionReceipt]:
        """C12: stop this turn's actions, not the session. What was admitted and has no receipt
        yet is receipted as cancelled_unknown -- input may already have reached the page --
        and nothing more is admitted until a fresh observation says where things stand."""
        session = self._require(session_id)
        receipted = {r.attempt_id for rs in session.receipts.values() for r in rs}
        receipts: list[ActionReceipt] = []
        for attempt_id, admission in list(session.admitted.items()):
            if attempt_id in receipted:
                continue
            receipt = ActionReceipt(admission.action.action_id, attempt_id, Outcome.CANCELLED_UNKNOWN, reason=reason)
            session.receipts.setdefault(receipt.action_id, []).append(receipt)
            receipts.append(receipt)
            risk = admission.action.risk.value
            if risk in {ActionClass.REVERSIBLE_WRITE.value, ActionClass.HOST.value} and not admission.effect_claim:
                session.unresolved[receipt.action_id] = receipt.outcome.value
            self._event(session, session_id, {"type": "receipt", "action_id": receipt.action_id, "attempt_id": attempt_id,
                                               "outcome": receipt.outcome.value, "reason": reason[:200], "risk": risk,
                                               "at": self._clock()})
        session.cancelled_until_observed = True
        self._event(session, session_id, {"type": "cancelled", "reason": reason[:200], "in_flight": len(receipts), "at": self._clock()})
        return receipts

    def dispatchable(self, session_id: str, attempt_id: str) -> bool:
        """G04 / C06: whether an admitted attempt may still send input now.

        Admission is checked once; an executor can then wait (a target still loading, an overlay) while a person
        takes the page, the owner revokes it, the turn is cancelled or the lease runs out. Asked again at the last
        moment before input: the attempt must still be admitted and unreceipted (a cancel receipts it), and the
        lease must still be the agent's.
        """
        session = self._sessions.get(session_id)
        if session is None or attempt_id not in session.admitted:
            return False
        if any(receipt.attempt_id == attempt_id for receipts in session.receipts.values() for receipt in receipts):
            return False
        return session.lease.agent_may_act(self._clock())

    # ── the door ───────────────────────────────────────────────────────

    def annotate(self, session_id: str, event: dict[str, Any]) -> bool:
        """A lifecycle note from a caller (a proposal, a hand-off): journaled like everything else."""
        session = self._require(session_id)
        return self._event(session, session_id, {**event, "at": event.get("at", self._clock())})

    def submit(self, session_id: str, payload: Any, *, effect_claim: str = "") -> Admission:
        """Admit one action. `effect_claim` is a durable Effect claim already held by the
        caller (the Effect Gateway's, after an approval); without it an external effect
        needs the configured claimer."""
        session = self._require(session_id)
        now = self._clock()
        try:
            action = payload if isinstance(payload, ComputerAction) else parse_action(payload)
        except ContractRefused as exc:
            self._refused(session, None, exc.reason, str(exc))
            raise GatewayRefused(exc.reason, str(exc)) from None
        try:
            self._check(session, action, now)
        except GatewayRefused as exc:
            self._refused(session, action, exc.reason, str(exc))
            raise
        claim = str(effect_claim or "").strip()
        if action.risk is ActionClass.EXTERNAL_EFFECT and not claim:
            if self._claim is None:
                self._refused(session, action, "external_effect_not_authorized",
                              "an external side effect needs an approved Effect claim before dispatch; none can be issued here")
                raise GatewayRefused("external_effect_not_authorized",
                                     f"{action.kind} is an external side effect and needs an approved Effect claim before it can be dispatched")
            claim = str(self._claim(action) or "")
            if not claim:
                self._refused(session, action, "effect_claim_missing")
                raise GatewayRefused("effect_claim_missing")
        admission = Admission(action=action, observation_hash=action.observation_hash, effect_claim=claim, admitted_at=now)
        recorded = self._event(session, session_id, {"type": "admitted", "action_id": action.action_id, "attempt_id": action.attempt_id,
                                                     "kind": action.kind, "risk": action.risk.value,
                                                     "fingerprint": action.fingerprint(), "at": now})
        if self._journal is not None and not recorded:
            if action.risk is not ActionClass.READ_ONLY:
                self._refused(session, action, "journal_unavailable", "the action could not be recorded durably; nothing was dispatched")
                raise GatewayRefused("journal_unavailable", "the action could not be recorded durably; nothing was dispatched")
            self._event(session, session_id, {"type": "journal_degraded", "action_id": action.action_id,
                                               "attempt_id": action.attempt_id, "at": now})
        session.admitted[action.attempt_id] = admission
        return admission

    def _check(self, session: _Session, action: ComputerAction, now: float) -> None:
        lease = session.lease
        if session.cancelled_until_observed:
            raise GatewayRefused("turn_cancelled", "the turn was cancelled; observe again before acting")
        if session.journal_unreadable and action.risk is not ActionClass.READ_ONLY:
            raise GatewayRefused("journal_unreadable", "the durable action journal cannot be read; writes are disabled")
        if action.scope_digest != lease.scope_digest:
            raise GatewayRefused("scope_mismatch")
        if action.control_lease_id != lease.lease_id:
            raise GatewayRefused("lease_mismatch")
        if not lease.agent_may_act(now):
            raise GatewayRefused("agent_not_in_control", lease.state.value)
        latest = session.latest_observation
        if latest is None:
            raise GatewayRefused("no_observation", "act on something observed")
        if action.observation_hash != latest.digest():
            raise GatewayRefused("stale_observation", "re-observe before acting")
        if latest.controller != "agent":
            raise GatewayRefused("observation_says_human_in_control", latest.controller)
        if action.geometry is not None:
            if not action.geometry.matches(latest.geometry):
                raise GatewayRefused("geometry_changed", "window, display, DPI or size differs from the observation")
            if action.geometry.expires_at and now >= action.geometry.expires_at:
                raise GatewayRefused("coordinate_expired")
        for host in action.egress_hosts:
            if not _host_allowed(host, lease.allowed_hosts):
                raise GatewayRefused("egress_not_allowed", host)
        if action.risk is ActionClass.EXTERNAL_EFFECT:
            if action.verification is None or not action.verification.independent:
                raise GatewayRefused("external_effect_without_independent_verification",
                                     f"needs one of {sorted(INDEPENDENT_SOURCES)}")
        history = session.receipts.get(action.action_id, [])
        if history:
            last = history[-1]
            if last.outcome is not Outcome.FAILED_BEFORE_EFFECT:
                raise GatewayRefused("retry_without_facts", f"previous attempt ended {last.outcome.value}; verify before a new attempt")
            if not action.retryable:
                raise GatewayRefused("action_not_retryable")
            # session.admitted is checked here too, not only on the fresh path (adversarial review, 2026-09-14):
            # a retry attempt that was already admitted but has not receipted yet must not be admitted a second
            # time, or the same attempt_id dispatches twice under one effect claim.
            if (action.attempt_id == last.attempt_id or action.attempt_id in session.spent_attempts
                    or action.attempt_id in session.admitted):
                raise GatewayRefused("attempt_id_reused")
        elif action.attempt_id in session.admitted or action.attempt_id in session.spent_attempts:
            raise GatewayRefused("attempt_id_reused")
        if action.risk in {ActionClass.REVERSIBLE_WRITE, ActionClass.HOST} and session.unresolved:
            outcomes = ", ".join(sorted(set(session.unresolved.values())))
            raise GatewayRefused("unresolved_outcome", f"an earlier write ended {outcomes}; take a browser_observe (or browser_snapshot) and check the page")

    # ── the way back ───────────────────────────────────────────────────

    def record(self, session_id: str, receipt: ActionReceipt) -> ActionReceipt:
        """An executor's result. `succeeded` needs independent evidence; labels are not outcomes."""
        session = self._require(session_id)
        admission = session.admitted.get(receipt.attempt_id)
        if admission is None or admission.action.action_id != receipt.action_id:
            raise GatewayRefused("receipt_for_unadmitted_attempt")
        history = session.receipts.get(receipt.action_id, [])
        if any(r.attempt_id == receipt.attempt_id and r.outcome is Outcome.CANCELLED_UNKNOWN for r in history):
            # The executor finished after the cancel: its word is kept as a late
            # note, but the attempt stays cancelled_unknown -- a cancel is not
            # undone by a result that arrived after it.
            self._event(session, session_id, {"type": "late_receipt", "action_id": receipt.action_id, "attempt_id": receipt.attempt_id,
                                               "outcome": receipt.outcome.value, "at": self._clock()})
            return history[-1]
        outcome = receipt.outcome
        if outcome is Outcome.SUCCEEDED:
            verification = receipt.evidence.get("verification") if isinstance(receipt.evidence, dict) else None
            sources = set(verification.get("sources", [])) if isinstance(verification, dict) else set()
            checked = verification.get("checked") if isinstance(verification, dict) else None
            held = isinstance(verification, dict) and verification.get("holds") is True
            if not (sources & INDEPENDENT_SOURCES and isinstance(checked, list) and checked and held):
                # An executor saying finished/verified/production is an input (C31), and so is a source it names
                # without a check that ran and held: a label is not a verification (§5.3).
                receipt = ActionReceipt(receipt.action_id, receipt.attempt_id, Outcome.OUTCOME_UNKNOWN,
                                        reason="executor_claimed_success_without_independent_verification",
                                        evidence=dict(receipt.evidence))
        session.receipts.setdefault(receipt.action_id, []).append(receipt)
        risk = admission.action.risk.value
        if not admission.effect_claim and GatewayJournal._locks_writes(receipt.outcome, receipt.reason, risk):
            session.unresolved[receipt.action_id] = receipt.outcome.value
        self._event(session, session_id, {"type": "receipt", "action_id": receipt.action_id, "attempt_id": receipt.attempt_id,
                                           "outcome": receipt.outcome.value, "reason": receipt.reason[:200], "risk": risk,
                                           "at": self._clock()})
        return receipt

    # ── helpers ────────────────────────────────────────────────────────

    def _require(self, session_id: str) -> _Session:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise GatewayRefused("unknown_session", session_id) from None

    def _refused(self, session: _Session, action: ComputerAction | None, reason: str, detail: str = "") -> None:
        self._event(session, session.lease.session_id, {"type": "refused", "reason": reason, "detail": detail[:200],
                                                         "action_id": getattr(action, "action_id", ""), "kind": getattr(action, "kind", ""),
                                                         "at": self._clock()})

    def _event(self, session: _Session, session_id: str, event: dict[str, Any]) -> bool:
        session.events.append(event)
        if self._journal is not None:
            return self._journal.append(session_id, event)
        return True
