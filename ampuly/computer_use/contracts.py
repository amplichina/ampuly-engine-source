"""Computer Use core contracts (4.3 §5): Observation, ComputerAction, ActionReceipt, VerificationSpec.

These are the strict shapes every executor -- structured browser, visual grounder,
desktop helper, workflow worker -- must speak to the SuperOnes Gateway. Nothing
here executes anything. The rules that matter most are the refusals:

- an action is one of an enumerated set of kinds, each with a fixed risk class;
  a free-form function name from an upstream parser (`run_code`, `evaluate`,
  `FINISHED`, ...) is not an action (G01, G06, C25, C31);
- an action binds to a scope, a control lease and the hash of the observation it
  was decided on; a coordinate action also binds to window/display/DPI geometry
  with an expiry (G05, C05, C30);
- an external side effect must carry a verification plan with at least one
  source independent of the model's own claim (§5.4, C11);
- a receipt's outcome is one of eight words, and `succeeded` is not the
  executor's to say without independent verification evidence (§5.3).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any


class ActionClass(str, Enum):
    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    EXTERNAL_EFFECT = "external_effect"
    HOST = "host"
    CONTROL = "control"


# The whole vocabulary. A kind not in this table does not exist, whatever an
# upstream parser or a model says it is called.
ACTION_KINDS: dict[str, ActionClass] = {
    "observe": ActionClass.READ_ONLY,
    "find": ActionClass.READ_ONLY,
    "scroll": ActionClass.READ_ONLY,
    "switch_tab": ActionClass.READ_ONLY,
    "copy_allowed_text": ActionClass.READ_ONLY,
    "type_draft": ActionClass.REVERSIBLE_WRITE,
    "open_settings": ActionClass.REVERSIBLE_WRITE,
    "select_candidate": ActionClass.REVERSIBLE_WRITE,
    "submit": ActionClass.EXTERNAL_EFFECT,
    "send": ActionClass.EXTERNAL_EFFECT,
    "save": ActionClass.EXTERNAL_EFFECT,
    "publish": ActionClass.EXTERNAL_EFFECT,
    "delete": ActionClass.EXTERNAL_EFFECT,
    "pay": ActionClass.EXTERNAL_EFFECT,
    "authorize": ActionClass.EXTERNAL_EFFECT,
    "launch_app": ActionClass.HOST,
    "switch_window": ActionClass.HOST,
    "pick_file": ActionClass.HOST,
    "clipboard": ActionClass.HOST,
    "terminal": ActionClass.HOST,
    "wait": ActionClass.CONTROL,
    "hand_to_human": ActionClass.CONTROL,
    "take_back": ActionClass.CONTROL,
    "cancel_turn": ActionClass.CONTROL,
    "revoke_session": ActionClass.CONTROL,
}

# Kinds whose target is a screen coordinate; they must carry geometry.
COORDINATE_KINDS = frozenset({"scroll", "type_draft", "select_candidate", "submit", "send", "save", "publish", "delete", "pay", "authorize"})

# Verification sources the model's own words cannot stand in for.
INDEPENDENT_SOURCES = frozenset({"url", "element", "field", "api", "download_hash", "db_event", "screenshot_region", "human"})


class Outcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED_BEFORE_EFFECT = "failed_before_effect"
    FAILED_AFTER_EFFECT = "failed_after_effect"
    OUTCOME_UNKNOWN = "outcome_unknown"
    AWAITING_HUMAN = "awaiting_human"
    CANCELLED_BEFORE_EFFECT = "cancelled_before_effect"
    CANCELLED_UNKNOWN = "cancelled_unknown"
    REFUSED = "refused"


def verified_outcome(verification: Any) -> tuple[Outcome, str] | None:
    """What a verification that actually ran establishes (§5.3), or None when it checked nothing.

    `verification` is {"sources", "checked", "holds", "failed"?, "unanswered"?}: every check held -> succeeded;
    the only misses are sources that did not answer -> outcome_unknown; an answered miss -> failed_after_effect.
    """
    if not isinstance(verification, dict) or not verification.get("checked"):
        return None
    if verification.get("holds") is True:
        if set(verification.get("sources", []) or []) & INDEPENDENT_SOURCES:
            return Outcome.SUCCEEDED, ""
        return Outcome.OUTCOME_UNKNOWN, "executor_claimed_success_without_independent_verification"
    unanswered = set(verification.get("unanswered", []) or [])
    if unanswered and not set(verification.get("failed", []) or []) - unanswered:
        return Outcome.OUTCOME_UNKNOWN, "verification_source_unavailable"
    return Outcome.FAILED_AFTER_EFFECT, "postcondition_not_met"


class ContractRefused(ValueError):
    """A payload that is not one of the contracts. `reason` is a stable code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _ident(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise ContractRefused("invalid_identifier", name)
    return value


@dataclass(frozen=True)
class Geometry:
    """What a coordinate was measured against. Any change invalidates the coordinate."""

    window_id: str
    display_id: str
    dpi_scale: float
    width: int
    height: int
    expires_at: float
    origin_x: int | None = None
    origin_y: int | None = None

    def matches(self, other: "Geometry | None") -> bool:
        return other is not None and (
            self.window_id, self.display_id, self.dpi_scale, self.width, self.height,
            self.origin_x, self.origin_y,
        ) == (
            other.window_id, other.display_id, other.dpi_scale, other.width, other.height,
            other.origin_x, other.origin_y,
        )


@dataclass(frozen=True)
class Observation:
    """The minimal structured view the Gateway lets a decision be made on (§5.1)."""

    session_id: str
    task_id: str
    scope_digest: str
    sequence: int
    url: str
    title: str
    domain_allowed: bool
    snapshot_hash: str
    controller: str  # "agent" | "human" | "none"
    lease_id: str
    verification_state: str
    produced_at: float
    previous_hash: str = ""
    redacted: bool = True
    geometry: Geometry | None = None
    screenshot_ref: str = ""

    def digest(self) -> str:
        """The observation hash an action binds to. Everything the decision could depend on."""
        payload = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "produced_at"}
        geometry = payload.pop("geometry")
        payload["geometry"] = None if geometry is None else {f.name: getattr(geometry, f.name) for f in fields(geometry)}
        return _digest(payload)


@dataclass(frozen=True)
class VerificationSpec:
    """How the outcome will be established (§5.4). Sources are names, not code."""

    sources: tuple[str, ...]

    @property
    def independent(self) -> bool:
        return any(source in INDEPENDENT_SOURCES for source in self.sources)


@dataclass(frozen=True)
class ComputerAction:
    action_id: str
    attempt_id: str
    kind: str
    scope_digest: str
    control_lease_id: str
    observation_hash: str
    target: dict[str, Any]
    expected_effect: str
    risk: ActionClass
    timeout_sec: float
    idempotent: bool
    retryable: bool
    egress_hosts: tuple[str, ...] = ()
    verification: VerificationSpec | None = None
    geometry: Geometry | None = None

    def fingerprint(self) -> str:
        """What an approval binds to: the action minus its attempt number (C09)."""
        payload = {
            "kind": self.kind, "scope_digest": self.scope_digest, "observation_hash": self.observation_hash,
            "target": self.target, "expected_effect": self.expected_effect, "egress_hosts": list(self.egress_hosts),
        }
        return _digest(payload)


@dataclass(frozen=True)
class ActionReceipt:
    action_id: str
    attempt_id: str
    outcome: Outcome
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


_ACTION_FIELDS = {f.name for f in fields(ComputerAction)}
_REQUIRED = {"action_id", "attempt_id", "kind", "scope_digest", "control_lease_id", "observation_hash", "target",
             "expected_effect", "risk", "timeout_sec", "idempotent", "retryable"}


def parse_action(payload: Any) -> ComputerAction:
    """Strict: unknown keys, unknown kinds and mismatched risk classes are refused, not coerced.

    An upstream parser accepting a string does not make it an action. `run_code`,
    `evaluate`, `FINISHED` and every other name outside ACTION_KINDS is refused here,
    before any executor sees it (G01, G06, C25).
    """
    if not isinstance(payload, dict):
        raise ContractRefused("action_not_an_object")
    unknown = set(payload) - _ACTION_FIELDS
    if unknown:
        raise ContractRefused("unknown_action_fields", ",".join(sorted(unknown)))
    missing = _REQUIRED - set(payload)
    if missing:
        raise ContractRefused("missing_action_fields", ",".join(sorted(missing)))
    kind = payload["kind"]
    if not isinstance(kind, str) or kind not in ACTION_KINDS:
        raise ContractRefused("unknown_action_kind", str(kind)[:64])
    expected_class = ACTION_KINDS[kind]
    risk = payload["risk"]
    try:
        declared = risk if isinstance(risk, ActionClass) else ActionClass(str(risk))
    except ValueError:
        raise ContractRefused("unknown_risk_class", str(risk)[:64]) from None
    if declared != expected_class:
        raise ContractRefused("risk_class_mismatch", f"{kind} is {expected_class.value}, declared {declared.value}")
    if not isinstance(payload["target"], dict):
        raise ContractRefused("target_not_an_object")
    for flag in ("idempotent", "retryable"):
        if type(payload[flag]) is not bool:
            raise ContractRefused("flag_not_boolean", flag)
    timeout = payload["timeout_sec"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
        raise ContractRefused("timeout_out_of_range")
    verification = payload.get("verification")
    if verification is not None and not isinstance(verification, VerificationSpec):
        if not isinstance(verification, dict) or not isinstance(verification.get("sources"), (list, tuple)):
            raise ContractRefused("verification_not_a_spec")
        verification = VerificationSpec(tuple(str(s) for s in verification["sources"]))
    geometry = payload.get("geometry")
    if geometry is not None and not isinstance(geometry, Geometry):
        if not isinstance(geometry, dict):
            raise ContractRefused("geometry_not_an_object")
        try:
            geometry = Geometry(**geometry)
        except TypeError as exc:
            raise ContractRefused("geometry_invalid", str(exc)[:80]) from None
    if kind in COORDINATE_KINDS and "x" in payload["target"] and geometry is None:
        raise ContractRefused("coordinate_without_geometry", kind)
    egress = payload.get("egress_hosts", ())
    if not isinstance(egress, (list, tuple)) or any(not isinstance(h, str) or not h for h in egress):
        raise ContractRefused("egress_hosts_invalid")
    return ComputerAction(
        action_id=_ident(payload["action_id"], "action_id"), attempt_id=_ident(payload["attempt_id"], "attempt_id"),
        kind=kind, scope_digest=_ident(payload["scope_digest"], "scope_digest"),
        control_lease_id=_ident(payload["control_lease_id"], "control_lease_id"),
        observation_hash=_ident(payload["observation_hash"], "observation_hash"),
        target=dict(payload["target"]), expected_effect=str(payload["expected_effect"])[:500], risk=declared,
        timeout_sec=float(timeout), idempotent=payload["idempotent"], retryable=payload["retryable"],
        egress_hosts=tuple(h.lower() for h in egress), verification=verification, geometry=geometry,
    )
