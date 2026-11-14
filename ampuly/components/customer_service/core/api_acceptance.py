"""Bound evidence of a successful WeCom API request, NOT delivery confirmation.

Only an installed adapter's trusted reader may supply these proofs. It must have
verified the relay signature, matched the pending request, and checked the actual
API response. A constructor, a hash, HTTP 200 or a wire ``passed`` flag performs
none of that authentication. This module checks exact immutable attribution.

The outbox retains the bounded proof in its existing audit journal and pins its
digest in immutable delivery evidence, without a schema migration or another DB.
No raw message, response, signature, access token or credential is retained here.
"""
from dataclasses import dataclass
from hashlib import sha256
import re

from .contracts import Context, bounded_text, canonical_json
from .delivery import command_body_digest

PROOF_VERSION = "ampuly.cs.api-acceptance/1"
MAX_PROOF_BYTES = 8192
PROOF_EVENT = "api_acceptance_proof"
PROOF_LEVEL = "api_accepted"
PROOF_SOURCE = "authenticated_relay_api"
_HASH = re.compile(r"[a-f0-9]{64}")
_ID = re.compile(r"[A-Za-z0-9_-]{1,192}")
_FIELDS = frozenset({"schema_version", "context", "command_id", "body_sha256",
    "request_msgid", "relay_request_id", "request_sha256", "response_sha256",
    "response_envelope_sha256", "response_msgid", "errcode"})


def _identifier(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError("api_acceptance_identifier_invalid")
    return value


def _hash(value):
    if type(value) is not str or not _HASH.fullmatch(value):
        raise ValueError("api_acceptance_digest_invalid")
    return value


@dataclass(frozen=True)
class ApiAcceptedReceipt:
    """Reference minted by a trusted adapter; never deserialize this from wire."""
    context: Context
    command_id: str
    proof_sha256: str

    def __post_init__(self):
        if type(self.context) is not Context:
            raise ValueError("api_acceptance_context_invalid")
        _identifier(self.command_id)
        _hash(self.proof_sha256)


def request_msgid(command_id):
    """Stable <=32-character API id; this is NOT a receipt or delivery proof."""
    return sha256(("ampuly.cs.reply:" + _identifier(command_id)).encode()).hexdigest()[:32]


def request_digest(command, msgid):
    """Hash the exact text payload, with no fit/truncation or recipient override."""
    if type(command) is not dict or type(command.get("context")) is not Context:
        raise ValueError("api_acceptance_command_invalid")
    context = command["context"]
    if context.channel != "wecom_cs" or msgid != request_msgid(command["command_id"]):
        raise ValueError("api_acceptance_request_invalid")
    text = bounded_text(command.get("text"), "outbound text", 8000)
    if len(text.encode("utf-8")) > 2048:
        raise ValueError("api_acceptance_request_too_large")
    payload = {"touser": context.buyer_id, "open_kfid": context.account_id,
               "msgtype": "text", "msgid": msgid, "text": {"content": text}}
    return sha256(canonical_json(payload)).hexdigest()


def proof_digest(proof):
    """Only hash the small fixed-field proof, never arbitrary nested wire JSON."""
    if type(proof) is not dict or proof.keys() != _FIELDS:
        raise ValueError("api_acceptance_proof_invalid")
    if proof.get("schema_version") != PROOF_VERSION:
        raise ValueError("api_acceptance_proof_invalid")
    context = proof.get("context")
    if type(context) is not dict or context.keys() != Context.__dataclass_fields__.keys():
        raise ValueError("api_acceptance_context_invalid")
    # Context validates the bounded scalar fields before JSON serialization.
    Context(**context)
    _identifier(proof.get("command_id"))
    _identifier(proof.get("relay_request_id"))
    for key in ("body_sha256", "request_sha256", "response_sha256", "response_envelope_sha256"):
        _hash(proof.get(key))
    for key in ("request_msgid", "response_msgid"):
        value = _identifier(proof.get(key))
        if len(value) > 32:
            raise ValueError("api_acceptance_message_id_invalid")
    if type(proof.get("errcode")) is not int or proof["errcode"] != 0:
        raise ValueError("api_acceptance_api_not_accepted")
    encoded = canonical_json(proof)
    if len(encoded) > MAX_PROOF_BYTES:
        raise ValueError("api_acceptance_proof_too_large")
    return sha256(encoded).hexdigest()


def validate_proof(command, receipt, proof):
    """Validate attribution; authentication is the installed reader's contract."""
    if type(receipt) is not ApiAcceptedReceipt:
        raise ValueError("api_acceptance_receipt_required")
    digest = proof_digest(proof)
    if (receipt.context != command["context"] or receipt.command_id != command["command_id"]
            or digest != receipt.proof_sha256 or proof["context"] != command["context"].to_dict()
            or proof["command_id"] != command["command_id"]
            or proof["body_sha256"] != command_body_digest(command)
            or proof["request_msgid"] != request_msgid(command["command_id"])
            or proof["response_msgid"] != proof["request_msgid"]
            or proof["request_sha256"] != request_digest(command, proof["request_msgid"])):
        raise ValueError("api_acceptance_identity_mismatch")
    revision = command.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("api_acceptance_revision_invalid")
    # Rebuild every permitted field so subsequent caller mutations cannot alter
    # the proof persisted by the runtime.
    frozen = {key: (dict(value) if key == "context" else value) for key, value in proof.items()}
    evidence = {"command_id": command["command_id"], "context": command["context"].to_dict(),
        "revision": revision, "in_reply_to": command["in_reply_to"],
        "body_sha256": command_body_digest(command), "level": PROOF_LEVEL,
        "source": PROOF_SOURCE, "receipt_id": digest, "not_sent_proof_id": None}
    return frozen, evidence
