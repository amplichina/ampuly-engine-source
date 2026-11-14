"""Durable contracts for production side effects.

The effect boundary deliberately owns no business process decisions.  It
records an already proposed action, proves that it was authorized, and makes
the provider outcome explicit without pretending to offer exactly-once I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class EffectState(str, Enum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    CLAIMED = "claimed"
    APPLIED = "applied"
    REJECTED = "rejected"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ApprovalType(str, Enum):
    TECHNICAL = "TechnicalApproval"
    BUSINESS = "BusinessGate"
    RELEASE = "ReleaseApproval"
    ACCEPTANCE = "Acceptance"


class SimulationStatus(str, Enum):
    """How much a dry run actually established.

    ``complete`` and ``preview_only`` used to be the same word, and they are
    not the same claim. A complete simulation asked the far side and got an
    answer -- the recipient exists, the endpoint accepts this shape. A preview
    rendered exactly what would be sent and checked nothing outside this
    process. Both are worth showing to somebody about to approve a send; only
    one of them is evidence that it will work, and the person deciding is
    entitled to know which they are looking at.
    """

    COMPLETE = "complete"
    PREVIEW_ONLY = "preview_only"
    INCOMPLETE = "incomplete"
    UNSUPPORTED = "unsupported"


class VerificationStatus(str, Enum):
    """Strength of post-apply evidence returned by an effect provider."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNSUPPORTED = "unsupported"


class ProviderOutcome(str, Enum):
    APPLIED = "applied"
    KNOWN_NO_EFFECT = "known_no_effect"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EffectRecord:
    effect_id: str
    provider: str
    kind: str
    payload: dict[str, Any]
    payload_hash: str
    idempotency_key: str
    state: EffectState
    approval_type: ApprovalType
    project_id: str = "default"
    run_id: str = ""
    customer_id: str = ""
    environment_id: str = ""
    correlation_id: str = ""
    causation_id: str = ""
    provider_supports_idempotency: bool = False
    claim_before_apply: bool = True
    authorization_id: str = ""
    authorized_by: str = ""
    claim_token: str = ""
    claimed_by: str = ""
    attempt_count: int = 0
    provider_receipt: dict[str, Any] = field(default_factory=dict)
    simulation_status: SimulationStatus = SimulationStatus.UNSUPPORTED
    simulation_details: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    created_at: str = ""
    submitted_at: str = ""
    authorized_at: str = ""
    claimed_at: str = ""
    resolved_at: str = ""

    @property
    def is_authorized(self) -> bool:
        return bool(self.authorization_id)

    @property
    def is_terminal(self) -> bool:
        return self.state in {
            EffectState.APPLIED,
            EffectState.REJECTED,
            EffectState.FAILED,
            EffectState.OUTCOME_UNKNOWN,
        }


@dataclass(frozen=True)
class EffectEvent:
    sequence: int
    event_id: str
    effect_id: str
    event_type: str
    from_state: str
    to_state: str
    payload: dict[str, Any]
    correlation_id: str
    causation_id: str
    created_at: str


@dataclass(frozen=True)
class EffectOutboxRecord:
    """One durable Effect Journal event awaiting business projection."""

    sequence: int
    outbox_id: str
    effect_event_id: str
    effect_id: str
    event_type: str
    status: str
    attempt_count: int
    last_error: str
    created_at: str
    published_at: str


@dataclass(frozen=True)
class ProviderResult:
    outcome: ProviderOutcome
    receipt: dict[str, Any] = field(default_factory=dict)
    detail: str = ""


@dataclass(frozen=True)
class EffectClaim:
    acquired: bool
    record: EffectRecord
    reason: str = ""


@dataclass(frozen=True)
class EffectExecutionResult:
    status: str
    record: EffectRecord
    provider_called: bool
    replay_noop: bool = False
    detail: str = ""
    verification_status: VerificationStatus = VerificationStatus.UNSUPPORTED
    verification_details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EffectVerification:
    """Read-only verification evidence for an already applied effect."""

    effect_id: str
    status: VerificationStatus
    details: dict[str, Any] = field(default_factory=dict)
    event_id: str = ""
    verified_at: str = ""


class EffectProvider(Protocol):
    """A provider never receives production credentials through the payload."""

    name: str
    supports_idempotency: bool

    async def apply(
        self,
        effect: EffectRecord,
        *,
        idempotency_key: str,
    ) -> ProviderResult:
        ...

    async def simulate(self, effect: EffectRecord) -> tuple[SimulationStatus, dict[str, Any]]:
        ...

    # Providers may implement a local/read-only verification. The gateway
    # records ``unsupported`` when this method is absent.
    async def verify(self, effect: EffectRecord) -> tuple[VerificationStatus, dict[str, Any]]:
        ...


class EffectJournalError(RuntimeError):
    pass


class EffectNotFoundError(EffectJournalError):
    pass


class EffectConflictError(EffectJournalError):
    pass


class InvalidEffectTransition(EffectJournalError):
    pass
