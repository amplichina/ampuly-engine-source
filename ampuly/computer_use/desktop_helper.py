"""Desktop ACI Helper contract (4.3 X04, first slice): what a low-privilege input process may be told, and how.

The real Windows helper is a separate low-privilege process (X04); until its
native input dependency passes the closure gates, the E track runs on
``FakeDesktopHelper``. Both speak this contract:

- a helper acts only on a **capability**: minted by the Gateway-side broker for
  one admitted attempt, bound to one atomic kind and to the window, display,
  DPI and size the action was decided on, single-use, expiring in seconds
  (G05, C30, C21). A moved window or a changed DPI is a refusal, never a
  re-mapped click.
- typed text never travels in a request, a log or the clipboard: the request
  carries a single-use ``text_ref`` and the helper takes the text from the
  ``SecureInputBroker`` at the last moment (G07, C27).
- a kill switch stops every later request; a helper that lost sight of what
  happened after sending input says ``unknown`` and nothing here retries it
  (G03, C28); ``applied`` is "input accepted". The helper's own screen hash
  afterwards is its word, kept as evidence; only a Gateway-side verifier that
  looked for the expected state lets the Gateway call the action ``succeeded``
  (§5.3, §5.6 -- an executor is not trusted to grade itself).
- no shell, no app launch, no clipboard, no file dialog through this door:
  the helper's vocabulary is the four atomic kinds below.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from ampuly.computer_use.contracts import ActionReceipt, Geometry, Outcome, verified_outcome
from ampuly.computer_use.gateway import Admission, ComputerUseGateway

HELPER_KINDS = frozenset({"select_candidate", "scroll", "type_draft", "wait"})
DEFAULT_TTL_SEC = 10.0
TEXT_REF_TTL_SEC = 30.0


class HelperRefused(PermissionError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class HelperCapability:
    token: str
    session_id: str
    action_id: str
    attempt_id: str
    kind: str
    geometry: Geometry
    observation_hash: str
    issued_at: float
    expires_at: float


@dataclass(frozen=True)
class HelperRequest:
    """One atomic action for the helper. No text, no credentials, no selectors of the page."""

    token: str
    kind: str
    target: dict[str, Any]
    text_ref: str = ""


@dataclass(frozen=True)
class HelperResult:
    status: str  # applied | refused | unknown | killed
    reason: str = ""
    input_sent: bool = False
    screen_hash_after: str = ""
    geometry_now: Geometry | None = None


@dataclass(frozen=True)
class HelperView:
    """What the desktop looks like before an action (4.7-A06 wiring): the app in front, its window title, and the
    geometry an action would bind to. No pixels and no typed text -- a name, a title, a size and a hash."""

    app: str
    title: str
    geometry: Geometry
    screen_hash: str


class DesktopHelper(Protocol):
    async def execute(self, request: HelperRequest) -> HelperResult: ...


class SecureInputBroker:
    """Holds text the model asked to type until the helper takes it. In memory, single use, short-lived, never logged."""

    def __init__(self, *, clock: Callable[[], float] = time.time, ttl_sec: float = TEXT_REF_TTL_SEC) -> None:
        self._clock, self._ttl = clock, ttl_sec
        self._texts: dict[str, tuple[str, float]] = {}

    def deposit(self, text: str) -> str:
        ref = "txt_" + secrets.token_urlsafe(12)
        self._texts[ref] = (str(text), self._clock() + self._ttl)
        return ref

    def take(self, ref: str) -> str:
        entry = self._texts.pop(ref, None)
        if entry is None:
            raise HelperRefused("text_ref_unknown_or_used")
        text, expires_at = entry
        if self._clock() >= expires_at:
            raise HelperRefused("text_ref_expired")
        return text

    def pending(self) -> int:
        return len(self._texts)


class HelperCapabilityBroker:
    """Mints one capability per admitted attempt and redeems it once."""

    def __init__(self, *, clock: Callable[[], float] = time.time, ttl_sec: float = DEFAULT_TTL_SEC) -> None:
        self._clock, self._ttl = clock, ttl_sec
        self._live: dict[str, HelperCapability] = {}
        self._spent: set[str] = set()
        self._events: list[dict[str, Any]] = []

    def issue(self, session_id: str, admission: Admission) -> HelperCapability:
        action = admission.action
        if action.kind not in HELPER_KINDS:
            raise HelperRefused("kind_not_for_helper", action.kind)
        if action.geometry is None and action.kind != "wait":
            raise HelperRefused("capability_without_geometry", action.kind)
        if any(c.attempt_id == action.attempt_id for c in self._live.values()) or action.attempt_id in self._spent:
            raise HelperRefused("capability_already_issued_for_attempt", action.attempt_id)
        now = self._clock()
        capability = HelperCapability(
            token="cap_" + secrets.token_urlsafe(18), session_id=session_id, action_id=action.action_id,
            attempt_id=action.attempt_id, kind=action.kind, geometry=action.geometry,
            observation_hash=admission.observation_hash, issued_at=now, expires_at=now + self._ttl,
        )
        self._live[capability.token] = capability
        self._events.append({"type": "capability_issued", "attempt_id": action.attempt_id, "kind": action.kind, "at": now})
        return capability

    def redeem(self, token: str, *, kind: str, geometry_now: Geometry | None) -> HelperCapability:
        """Spend the capability, or say why not. Spent either way: a refused token is not retried."""
        capability = self._live.pop(str(token or ""), None)
        if capability is None:
            raise HelperRefused("capability_unknown_or_used")
        self._spent.add(capability.attempt_id)
        now = self._clock()
        if now >= capability.expires_at:
            self._events.append({"type": "capability_refused", "reason": "expired", "attempt_id": capability.attempt_id, "at": now})
            raise HelperRefused("capability_expired")
        if kind != capability.kind:
            self._events.append({"type": "capability_refused", "reason": "kind_mismatch", "attempt_id": capability.attempt_id, "at": now})
            raise HelperRefused("capability_kind_mismatch", f"{kind} != {capability.kind}")
        if not capability.geometry.matches(geometry_now):
            self._events.append({"type": "capability_refused", "reason": "geometry_changed", "attempt_id": capability.attempt_id, "at": now})
            raise HelperRefused("geometry_changed", "window, display, DPI or size differs from what the action was decided on")
        self._events.append({"type": "capability_redeemed", "attempt_id": capability.attempt_id, "at": now})
        return capability

    def inspect(self, token: str) -> HelperCapability:
        """Read authority metadata without consuming it.

        The independent desktop worker needs the expected geometry before it
        can report the live geometry used by ``redeem``. This method exposes
        no token enumeration and does not extend expiry or usage.
        """
        capability = self._live.get(str(token or ""))
        if capability is None:
            raise HelperRefused("capability_unknown_or_used")
        return capability

    def discard(self, token: str, *, reason: str) -> bool:
        """Consume unused authority after a worker refusal/crash."""
        capability = self._live.pop(str(token or ""), None)
        if capability is None:
            return False
        self._spent.add(capability.attempt_id)
        self._events.append({
            "type": "capability_discarded",
            "attempt_id": capability.attempt_id,
            "reason": str(reason or "worker_ended")[:80],
            "at": self._clock(),
        })
        return True

    def events(self) -> list[dict[str, Any]]:
        return list(self._events)


@dataclass
class FakeDesktopHelper:
    """The E-track helper: a screen made of a Geometry, inputs recorded without text, a kill switch."""

    broker: HelperCapabilityBroker
    texts: SecureInputBroker
    screen: Geometry
    killed: bool = False
    crash_after_send: bool = False
    inputs: list[dict[str, Any]] = field(default_factory=list)
    typed: list[str] = field(default_factory=list)  # what reached the "screen", for tests only; never logged
    # 4.7-A06: which app is in front and what its window says, for a flow step's allowed apps and done check.
    app: str = "fake-app.exe"
    title: str = ""

    def kill(self) -> None:
        self.killed = True

    def observe(self) -> HelperView:
        """The desktop as a flow step reads it before acting (the product's desktop bridge calls this)."""
        return HelperView(app=self.app, title=self.title, geometry=self.screen, screen_hash=self.screen_hash())

    def move_window(self, **changes: Any) -> None:
        self.screen = Geometry(**{**self.screen.__dict__, **changes})

    def screen_hash(self) -> str:
        return hashlib.sha256(f"{self.screen.window_id}|{self.screen.display_id}|{len(self.inputs)}".encode()).hexdigest()

    async def execute(self, request: HelperRequest) -> HelperResult:
        if self.killed:
            return HelperResult(status="killed", reason="kill_switch")
        try:
            capability = self.broker.redeem(request.token, kind=request.kind, geometry_now=self.screen)
        except HelperRefused as exc:
            return HelperResult(status="refused", reason=exc.reason, geometry_now=self.screen)
        record: dict[str, Any] = {"kind": request.kind, "attempt_id": capability.attempt_id}
        if request.kind == "type_draft":
            try:
                text = self.texts.take(request.text_ref)
            except HelperRefused as exc:
                return HelperResult(status="refused", reason=exc.reason, geometry_now=self.screen)
            self.typed.append(text)
            record["length"] = len(text)
        else:
            record["target"] = {k: v for k, v in request.target.items() if k in {"x", "y", "click", "direction", "to", "seconds"}}
        self.inputs.append(record)
        if self.crash_after_send:
            return HelperResult(status="unknown", reason="helper_lost_track_after_input", input_sent=True, geometry_now=self.screen)
        return HelperResult(status="applied", input_sent=True, screen_hash_after=self.screen_hash(), geometry_now=self.screen)


async def run_through_helper(gateway: ComputerUseGateway, session_id: str, admission: Admission, helper: DesktopHelper,
                             broker: HelperCapabilityBroker, *, text_ref: str = "",
                             verifier: Callable[[Admission, HelperResult], Awaitable[dict[str, Any] | None]] | None = None) -> ActionReceipt:
    """Admitted action -> capability -> helper -> receipt recorded on the Gateway. No retry anywhere in here.

    `verifier` runs on the Gateway side after applied input: its own observation held against the expected state,
    as {"sources", "checked", "holds", "failed"?, "unanswered"?}. Without one, applied input is outcome_unknown.
    """
    action = admission.action
    try:
        capability = broker.issue(session_id, admission)
    except HelperRefused as exc:
        return gateway.record(session_id, ActionReceipt(action.action_id, action.attempt_id, Outcome.REFUSED, reason=exc.reason))
    request = HelperRequest(token=capability.token, kind=action.kind, target=dict(action.target), text_ref=text_ref)
    result = await helper.execute(request)
    if result.status == "applied":
        evidence: dict[str, Any] = {"input_sent": True}
        if result.screen_hash_after:
            evidence["screen_hash_after"] = result.screen_hash_after
        verification = await verifier(admission, result) if verifier is not None else None
        verified = verified_outcome(verification)
        if verified is not None:
            receipt = ActionReceipt(action.action_id, action.attempt_id, verified[0], reason=verified[1],
                                    evidence={**evidence, "verification": dict(verification or {})})
        else:
            receipt = ActionReceipt(action.action_id, action.attempt_id, Outcome.OUTCOME_UNKNOWN, reason="input_accepted_unverified",
                                    evidence=evidence)
    elif result.status == "refused":
        receipt = ActionReceipt(action.action_id, action.attempt_id, Outcome.REFUSED, reason=result.reason)
    elif result.status == "killed":
        receipt = ActionReceipt(action.action_id, action.attempt_id, Outcome.CANCELLED_BEFORE_EFFECT if not result.input_sent else Outcome.CANCELLED_UNKNOWN,
                                reason=result.reason)
    else:
        receipt = ActionReceipt(action.action_id, action.attempt_id, Outcome.OUTCOME_UNKNOWN, reason=result.reason or "helper_unknown",
                                evidence={"input_sent": result.input_sent})
    return gateway.record(session_id, receipt)
