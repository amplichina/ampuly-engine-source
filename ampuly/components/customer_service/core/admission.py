"""Pure admission requests and evidence binding; no evidence store or grants.

The host must supply an evidence reader backed by its authenticated persistent
test and acceptance records. That boundary checks the actor's authority and
record revocation before returning evidence. Client flags, model output, and
synthetic fixtures are not acceptance evidence. This module cannot authenticate
a reader and intentionally provides no default or in-memory production source.
"""
from dataclasses import dataclass
from hashlib import sha256
import re
from typing import Callable

from .autonomy import AdmissionSnapshot, CurrentBasis
from .contracts import bounded_text, canonical_json
from .understanding import INTENT_CATALOG


SCHEMA_VERSION = "ampuly.cs.admission-request/1"
_SCOPE = ("tenant_id", "shop_id", "channel", "account_id")
_BASIS = ("package_sha256", "knowledge_sha256", "model_sha256", "policy_sha256")
_FIELDS = frozenset({"schema_version", "admission_id", "scope", "basis", "valid_from",
                     "valid_until", "allowed_intents", "max_clarification_turns",
                     "authorized_reply_actions", "max_auto_messages"})
_HASH = re.compile(r"[a-f0-9]{64}")


class AdmissionError(ValueError):
    """Stable non-sensitive codes, never contents of requests or evidence."""


def _identifier(value, code):
    if type(value) is not str or not 1 <= len(value) <= 128:
        raise AdmissionError(code)
    try:
        bounded_text(value, "admission identifier", 128)
        value.encode("utf-8")
    except (ValueError, UnicodeError):
        raise AdmissionError(code) from None
    if value != value.strip() or "\n" in value or "\t" in value or "\x7f" in value:
        raise AdmissionError(code)


def _strings(value, maximum):
    """Copy only bounded built-in containers, never arbitrary iterators."""
    if (type(value) not in (list, tuple, set, frozenset) or len(value) > maximum
            or any(type(item) is not str or not 1 <= len(item) <= 128 for item in value)):
        raise AdmissionError("admission_request_invalid")
    normalized = frozenset(value)
    if len(normalized) != len(value):
        raise AdmissionError("admission_request_invalid")
    return normalized


def _object(value, fields):
    if (type(value) is not dict or len(value) != len(fields)
            or any(type(key) is not str or not 1 <= len(key) <= 128 for key in value)
            or value.keys() != fields):
        raise AdmissionError("admission_request_invalid")


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    """Immutable conditions awaiting independent AI and human acceptance."""
    admission_id: str
    tenant_id: str
    shop_id: str
    channel: str
    account_id: str
    package_sha256: str
    knowledge_sha256: str
    model_sha256: str
    policy_sha256: str
    valid_from: int
    valid_until: int
    allowed_intents: frozenset[str]
    max_clarification_turns: int
    authorized_reply_actions: frozenset[str]
    max_auto_messages: int

    def __post_init__(self):
        for name in ("admission_id", *_SCOPE):
            _identifier(getattr(self, name), "admission_request_invalid")
        object.__setattr__(self, "allowed_intents", _strings(self.allowed_intents, len(INTENT_CATALOG)))
        object.__setattr__(self, "authorized_reply_actions", _strings(self.authorized_reply_actions, 2))
        if type(self.max_auto_messages) is not int or not 1 <= self.max_auto_messages <= 100000:
            raise AdmissionError("admission_request_invalid")
        try:
            CurrentBasis(**{name: getattr(self, name) for name in _BASIS})
            self.snapshot()
        except (ValueError, TypeError):
            raise AdmissionError("admission_request_invalid") from None

    def as_dict(self):
        """Return detached, bounded JSON data with deterministic collection order."""
        return {"schema_version": SCHEMA_VERSION, "admission_id": self.admission_id,
                "scope": {name: getattr(self, name) for name in _SCOPE},
                "basis": {name: getattr(self, name) for name in _BASIS},
                "valid_from": self.valid_from, "valid_until": self.valid_until,
                "allowed_intents": sorted(self.allowed_intents),
                "max_clarification_turns": self.max_clarification_turns,
                "authorized_reply_actions": sorted(self.authorized_reply_actions),
                "max_auto_messages": self.max_auto_messages}

    @classmethod
    def from_dict(cls, value):
        """Require the exact JSON schema; flags cannot assert acceptance."""
        _object(value, _FIELDS)
        if type(value["schema_version"]) is not str or value["schema_version"] != SCHEMA_VERSION:
            raise AdmissionError("admission_request_invalid")
        _object(value["scope"], frozenset(_SCOPE))
        _object(value["basis"], frozenset(_BASIS))
        for name in ("allowed_intents", "authorized_reply_actions"):
            if type(value[name]) is not list:
                raise AdmissionError("admission_request_invalid")
        return cls(admission_id=value["admission_id"], **value["scope"], **value["basis"],
                   valid_from=value["valid_from"], valid_until=value["valid_until"],
                   allowed_intents=value["allowed_intents"],
                   max_clarification_turns=value["max_clarification_turns"],
                   authorized_reply_actions=value["authorized_reply_actions"],
                   max_auto_messages=value["max_auto_messages"])

    @property
    def digest(self):
        """Bind every requested condition, including scope, time and quota."""
        return sha256(canonical_json(self.as_dict())).hexdigest()

    def snapshot(self):
        """Project validated facts; the host may use this only after evidence verification.

        The true acceptance flags are not proof or a grant. Construction also
        uses this projection solely to reuse existing structural validation.
        The host must persist and enforce max_auto_messages separately.
        """
        return AdmissionSnapshot(
            admission_id=self.admission_id, **{name: getattr(self, name) for name in _SCOPE},
            **{name: getattr(self, name) for name in _BASIS},
            ai_tests_passed=True, human_acceptance_passed=True, revoked=False,
            valid_from=self.valid_from, valid_until=self.valid_until,
            allowed_intents=self.allowed_intents, max_clarification_turns=self.max_clarification_turns,
            authorized_reply_actions=self.authorized_reply_actions)


@dataclass(frozen=True, slots=True)
class AcceptanceEvidence:
    """Detached facts from an authenticated, authorized, non-revoked host record."""
    reference: str
    kind: str
    request_sha256: str
    record_sha256: str
    actor_id: str
    passed: bool

    def __post_init__(self):
        for value in (self.reference, self.actor_id):
            _identifier(value, "admission_evidence_invalid")
        if type(self.kind) is not str or self.kind not in {"ai_test", "human_acceptance"}:
            raise AdmissionError("admission_evidence_invalid")
        for value in (self.request_sha256, self.record_sha256):
            if type(value) is not str or not _HASH.fullmatch(value):
                raise AdmissionError("admission_evidence_invalid")
        if type(self.passed) is not bool:
            raise AdmissionError("admission_evidence_invalid")


def verify_evidence(request: AdmissionRequest, ai_reference: str, human_reference: str,
                    evidence_reader: Callable[[str], AcceptanceEvidence | None]
                    ) -> tuple[AcceptanceEvidence, AcceptanceEvidence]:
    """Read and bind two independent evidence kinds using a trusted host callback.

    The callback must enforce persistent-record authenticity, actor authority,
    and revocation. It must never construct evidence from client acceptance
    flags. Persist both returned record_sha256 values with the admission and
    compare them against fresh authenticated reads before later use so changed
    records invalidate admission. Synthetic test readers prove no real acceptance.
    """
    if type(request) is not AdmissionRequest:
        raise AdmissionError("admission_request_invalid")
    for reference in (ai_reference, human_reference):
        _identifier(reference, "admission_evidence_invalid")
    if ai_reference == human_reference or not callable(evidence_reader):
        raise AdmissionError("admission_evidence_invalid")
    digest = request.digest
    verified = []
    for reference, kind in ((ai_reference, "ai_test"), (human_reference, "human_acceptance")):
        try:
            evidence = evidence_reader(reference)
        except Exception:
            raise AdmissionError("admission_evidence_unavailable") from None
        if type(evidence) is not AcceptanceEvidence:
            raise AdmissionError("admission_evidence_invalid")
        evidence.__post_init__()
        if (evidence.reference != reference or evidence.kind != kind
                or evidence.request_sha256 != digest or evidence.passed is not True):
            raise AdmissionError("admission_evidence_invalid")
        verified.append(evidence)
    return verified[0], verified[1]
