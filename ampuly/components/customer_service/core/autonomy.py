"""Host admission-based reply eligibility; no approval, storage or sending.

The host MUST first authenticate the persistent admission, independent AI tests,
human acceptance, and current package/knowledge/DeepSeek-model/policy hashes.
Snapshots are facts, not authentication or grants; never accept them from model,
customer or browser trust flags. Dispatch still checks live grant/Stop/budget
and exact-command fencing. Unknown cost stays unknown; no cost is computed here.
Protected operations need a future separate action authorization; classifying a
refund request here is not a claim that all refund discussion forever needs a human.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import re

from .contracts import Context, bounded_text, canonical_json
from .conversation import _turns, _validate, response_schema
from .understanding import INTENT_CATALOG, check_understanding


_HASH = re.compile(r"[a-f0-9]{64}")
_REPLY_ACTIONS = frozenset({"auto_reply", "auto_clarify"})
_FINGERPRINTS = ("package_sha256", "knowledge_sha256", "model_sha256", "policy_sha256")
_SCOPE = ("tenant_id", "shop_id", "channel", "account_id")
_PROTECTED = frozenset({"refund", "exchange", "cancel_order", "delivery_change"})
_RISK_REASONS = ("customer_requested_human", "safety_risk", "complaint", "protected_action",
                 "repeated_failure", "unresolved_strong_emotion", "knowledge_conflict")
_DRAFT_SCHEMA = response_schema(structured_understanding=True)


class AutonomyError(ValueError):
    """Stable non-sensitive input code, never customer/provider content."""


def _text(value):
    if type(value) is not str:
        raise AutonomyError("autonomy_input_invalid")
    try:
        bounded_text(value, "autonomy identifier", 128)
    except ValueError:
        raise AutonomyError("autonomy_input_invalid") from None
    if value != value.strip() or "\n" in value or "\t" in value:
        raise AutonomyError("autonomy_input_invalid")


def _hash(value):
    if type(value) is not str or not _HASH.fullmatch(value):
        raise AutonomyError("autonomy_fingerprint_invalid")


def _integer(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise AutonomyError("autonomy_input_invalid")


@dataclass(frozen=True, slots=True)
class AdmissionSnapshot:
    """Detached facts read from an authenticated host record, not a new grant."""
    admission_id: str
    tenant_id: str
    shop_id: str
    channel: str
    account_id: str
    package_sha256: str
    knowledge_sha256: str
    model_sha256: str
    policy_sha256: str
    ai_tests_passed: bool
    human_acceptance_passed: bool
    revoked: bool
    valid_from: int
    valid_until: int
    allowed_intents: frozenset[str]
    max_clarification_turns: int
    authorized_reply_actions: frozenset[str]

    def __post_init__(self):
        for name in ("admission_id", *_SCOPE):
            _text(getattr(self, name))
        for name in _FINGERPRINTS:
            _hash(getattr(self, name))
        if any(type(getattr(self, name)) is not bool for name in
               ("ai_tests_passed", "human_acceptance_passed", "revoked")):
            raise AutonomyError("autonomy_input_invalid")
        for value in (self.valid_from, self.valid_until):
            _integer(value, 253402300799)
        if self.valid_until <= self.valid_from:
            raise AutonomyError("autonomy_input_invalid")
        _integer(self.max_clarification_turns, 10)
        if (type(self.allowed_intents) is not frozenset or not 1 <= len(self.allowed_intents) <= len(INTENT_CATALOG)
                or any(type(name) is not str or name not in INTENT_CATALOG for name in self.allowed_intents)
                or type(self.authorized_reply_actions) is not frozenset
                or not self.authorized_reply_actions <= _REPLY_ACTIONS):
            raise AutonomyError("autonomy_input_invalid")


@dataclass(frozen=True, slots=True)
class CurrentBasis:
    """Host-computed current fingerprints, never values asserted by the model."""
    package_sha256: str
    knowledge_sha256: str
    model_sha256: str
    policy_sha256: str

    def __post_init__(self):
        for name in _FINGERPRINTS:
            _hash(getattr(self, name))


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: str
    reason_codes: tuple[str, ...]
    reply_sha256: str | None
    admission_id: str | None
    requires_human: bool = field(init=False)
    sending_authorized: bool = field(default=False, init=False)

    def __post_init__(self):
        object.__setattr__(self, "requires_human", self.action == "handoff")


def _bounded_json(value):
    """Bound traversal before schema checks, hashing or copying nested input."""
    nodes, characters = 0, 0

    def visit(item, depth):
        nonlocal nodes, characters
        nodes += 1
        if nodes > 4096 or depth > 12:
            raise AutonomyError("autonomy_candidate_invalid")
        kind = type(item)
        if kind is str:
            characters += len(item)
            if len(item) > 8000 or characters > 200000:
                raise AutonomyError("autonomy_candidate_invalid")
        elif kind is dict:
            if len(item) > 64 or any(type(key) is not str or len(key) > 128 for key in item):
                raise AutonomyError("autonomy_candidate_invalid")
            for child in item.values():
                visit(child, depth + 1)
        elif kind is list:
            if len(item) > 64:
                raise AutonomyError("autonomy_candidate_invalid")
            for child in item:
                visit(child, depth + 1)
        elif kind is int:
            if abs(item) > 2**63 - 1:
                raise AutonomyError("autonomy_candidate_invalid")
        elif item is not None and kind is not bool:
            raise AutonomyError("autonomy_candidate_invalid")

    visit(value, 0)


def _candidate(candidate):
    """Defensive checks on the already host-validated service result."""
    _bounded_json(candidate)
    if (type(candidate) is not dict or candidate.get("kind") != "ampuly.cs.conversation-draft/1"
            or candidate.get("result_status") not in {"draft", "fallback", "denied"}
            or candidate.get("mode") not in {"model-candidate", "safe-fallback"}
            or candidate.get("review_required") is not True
            or candidate.get("customer_claims_verified") is not False
            or candidate.get("semantic_quality_verified") is not False
            or candidate.get("case_status") != "open"
            or type(candidate.get("requires_human")) is not bool
            or type(candidate.get("model_calls")) is not int or not 0 <= candidate["model_calls"] <= 1
            or type(candidate.get("validation_errors")) is not list or len(candidate["validation_errors"]) > 32
            or any(type(code) is not str or not 1 <= len(code) <= 128 for code in candidate["validation_errors"])
            or type(candidate.get("sources")) is not list or len(candidate["sources"]) > 14):
        raise AutonomyError("autonomy_candidate_invalid")
    _hash(candidate.get("context_key"))
    _hash(candidate.get("history_hash"))
    draft, handoff = candidate.get("draft"), candidate.get("handoff")
    _validate(draft, _DRAFT_SCHEMA)
    if (type(handoff) is not dict or type(handoff.get("required")) is not bool
            or handoff.get("status") not in {"pending_assignment", "not_requested"}
            or type(handoff.get("reasons")) is not list or len(handoff["reasons"]) > 32
            or any(type(code) is not str or not 1 <= len(code) <= 200 for code in handoff["reasons"])):
        raise AutonomyError("autonomy_candidate_invalid")
    turns = _turns(handoff.get("conversation"))
    check_understanding(draft["understanding"], turns)
    return draft, handoff, sha256(canonical_json(turns)).hexdigest()


def evaluate_autonomy(*, admission: AdmissionSnapshot | None, basis: CurrentBasis,
                      context: Context, candidate: dict, expected_history_hash: str,
                      now: int, clarification_turns: int) -> PolicyDecision:
    """Choose eligibility using the original host-validated v0.4 result.

    ``now`` is host UTC Unix seconds; ``clarification_turns`` is its persisted count
    of already issued clarification turns, never a model-resettable count. Each
    clarification has at most two questions. Every decision remains non-sending.
    """
    if (type(basis) is not CurrentBasis or type(context) is not Context
            or admission is not None and type(admission) is not AdmissionSnapshot):
        raise AutonomyError("autonomy_input_invalid")
    _integer(now, 253402300799)
    _integer(clarification_turns, 1000)
    _hash(expected_history_hash)
    try:
        context_key = context.key
    except (ValueError, TypeError):
        raise AutonomyError("autonomy_input_invalid") from None
    reply_digest = None

    def decide(action, *codes):
        return PolicyDecision(action, tuple(codes), reply_digest,
                              None if admission is None else admission.admission_id)

    if admission is None:
        return decide("hold", "autonomy_admission_missing")
    if admission.revoked:
        return decide("hold", "autonomy_admission_revoked")
    if not admission.ai_tests_passed or not admission.human_acceptance_passed:
        return decide("hold", "autonomy_admission_not_passed")
    if now < admission.valid_from or now >= admission.valid_until:
        return decide("hold", "autonomy_admission_not_current")
    if any(getattr(admission, name) != getattr(context, name) for name in _SCOPE):
        return decide("hold", "autonomy_admission_scope_mismatch")
    if any(getattr(admission, name) != getattr(basis, name) for name in _FINGERPRINTS):
        return decide("hold", "autonomy_basis_changed")
    if not admission.authorized_reply_actions:
        return decide("hold", "autonomy_reply_scope_denied")
    try:
        draft, handoff, actual_history_hash = _candidate(candidate)
        reply_digest = sha256(draft["reply"].encode("utf-8")).hexdigest()
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return decide("handoff", "autonomy_candidate_invalid")
    if candidate["context_key"] != context_key:
        return decide("hold", "autonomy_candidate_context_mismatch")
    if candidate["history_hash"] != expected_history_hash or actual_history_hash != expected_history_hash:
        return decide("hold", "autonomy_candidate_history_mismatch")
    if candidate.get("effects_produced") != [] or handoff.get("executed_actions") != []:
        return decide("hold", "autonomy_effects_present")
    if candidate["result_status"] == "denied":
        return decide("hold", "autonomy_candidate_scope_denied")
    if candidate["result_status"] != "draft" or candidate["mode"] != "model-candidate" or candidate["validation_errors"]:
        return decide("handoff", "autonomy_candidate_fallback")
    if candidate["model_calls"] != 1:
        return decide("handoff", "autonomy_model_call_unconfirmed")
    risk = tuple("autonomy_" + reason for reason in _RISK_REASONS if reason in handoff["reasons"])
    if risk:
        return decide("handoff", *risk)
    # v0.4 hypotheses and review_required are informational, not handoff flags.
    if (draft["handoff"]["requested"] or draft["next_action"] in {"handoff", "verify_identity"}
            or candidate["requires_human"] or handoff["required"]):
        return decide("handoff", "autonomy_handoff_requested")
    live = [intent for intent in draft["understanding"]["intents"]
            if intent["disposition"] in {"active", "uncertain"}]
    if any(intent["name"] in _PROTECTED for intent in live):
        return decide("handoff", "autonomy_protected_action")
    if any(intent["name"] in {"human_handoff", "complaint"} for intent in live):
        return decide("handoff", "autonomy_customer_escalation")
    if not live or any(intent["name"] == "other" for intent in live):
        return decide("handoff", "autonomy_intent_unresolved")
    if any(intent["name"] not in admission.allowed_intents for intent in live):
        return decide("handoff", "autonomy_intent_not_admitted")
    clarify = (any(intent["missing_slots"] or intent["disposition"] == "uncertain" for intent in live)
               or draft["next_action"] == "clarify" or bool(draft["questions"]))
    if clarify:
        if clarification_turns >= admission.max_clarification_turns:
            return decide("handoff", "autonomy_clarification_limit")
        if not draft["questions"] or any(question not in draft["reply"] for question in draft["questions"]):
            return decide("handoff", "autonomy_clarification_missing")
        action, reason = "auto_clarify", "autonomy_clarification_needed"
    else:
        source_ids = {source["id"] for source in candidate["sources"]
                      if type(source) is dict and type(source.get("id")) is str}
        if not draft["citations"] or any(citation["source_id"] not in source_ids for citation in draft["citations"]):
            return decide("handoff", "autonomy_reply_evidence_missing")
        action, reason = "auto_reply", "autonomy_admitted_reply"
    if action not in admission.authorized_reply_actions:
        return decide("hold", "autonomy_reply_scope_denied")
    return decide(action, reason)
