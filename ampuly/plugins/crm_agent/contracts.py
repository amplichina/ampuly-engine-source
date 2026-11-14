"""CRM records are business evidence, never execution instructions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re


class CRMError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fail(code):
    raise CRMError("crm_" + code)


def text(value, *, limit=2000, required=False):
    if type(value) is not str or len(value) > limit or "\x00" in value:
        fail("input_invalid")
    result = value.strip()
    if required and not result:
        fail("input_invalid")
    return result


def identifier(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is None:
        fail("input_invalid")
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Scope:
    """Snapshot of the engine's authoritative binding, never CRM identity.

    Sales company/contact IDs identify prospects inside this operating scope.
    They neither create nor replace engine customers, accounts or projects.
    The live host must revalidate this snapshot before every operation.
    """
    project_id: str
    customer_id: str
    actor: str
    can_write: bool
    owner_ids: tuple[str, ...]

    def check(self, *, write=False):
        identifier(self.project_id)
        identifier(self.customer_id)
        text(self.actor, limit=128, required=True)
        if write and self.can_write is not True:
            fail("write_forbidden")


PIPELINE_STATUSES = ("lead", "qualified", "proposal", "won", "lost")
KINDS = ("company", "contact", "opportunity", "activity")
FIELDS = {
    "company": {"name", "website", "notes", "owner_id"},
    "contact": {"name", "company_id", "email", "phone", "notes", "owner_id"},
    "opportunity": {"name", "company_id", "contact_id", "pipeline_status", "amount_minor", "currency",
                    "next_action", "next_due", "notes", "owner_id"},
    "activity": {"name", "opportunity_id", "notes", "occurred_at", "source_reference", "owner_id"},
}


def validate_fields(kind, payload, scope):
    if kind not in KINDS or type(payload) is not dict or set(payload) != FIELDS[kind]:
        fail("input_invalid")
    result = {}
    for key, value in payload.items():
        if key == "amount_minor":
            if type(value) is not int or not 0 <= value <= 10**14:
                fail("amount_invalid")
            result[key] = value
        else:
            result[key] = text(value, limit=4000 if key == "notes" else 500,
                               required=key in {"name", "owner_id", "currency", "pipeline_status"})
    if result["owner_id"] not in scope.owner_ids:
        fail("owner_invalid")
    for key in ("company_id", "contact_id", "opportunity_id"):
        if result.get(key):
            identifier(result[key])
    for key in ("occurred_at", "next_due"):
        if result.get(key):
            try:
                stamp = datetime.fromisoformat(result[key].replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    fail("date_invalid")
            except ValueError:
                fail("date_invalid")
    if kind == "opportunity":
        if result["pipeline_status"] not in PIPELINE_STATUSES:
            fail("pipeline_status_invalid")
        if result["currency"] not in {"CNY", "USD", "EUR", "GBP", "JPY", "HKD"}:
            fail("currency_invalid")
        if not result["company_id"]:
            fail("company_required")
    if kind == "contact" and result["email"] and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", result["email"]) is None:
        fail("email_invalid")
    if result.get("website") and not re.fullmatch(r"https?://[^\s]+", result["website"]):
        fail("website_invalid")
    return result


# Synthetic-only admission (default and, in this build, the only mode).
# Structured contact channels must be visibly fictional: reserved example
# domains (RFC 2606 / RFC 6761) and the reserved 555-01xx telephone range.
# Free text (names, notes, next steps) cannot be machine-verified as
# synthetic; the owner enable step states that only synthetic data is allowed.
RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
RESERVED_SUFFIXES = ("example", "test", "invalid", "localhost")


def reserved_host(host):
    host = host.strip().rstrip(".").casefold()
    if not host or re.fullmatch(r"[a-z0-9.-]{1,253}", host) is None:
        return False
    labels = host.split(".")
    return labels[-1] in RESERVED_SUFFIXES or any(host == d or host.endswith("." + d) for d in RESERVED_DOMAINS)


def require_synthetic(kind, fields):
    email = fields.get("email", "")
    if email and not reserved_host(email.rpartition("@")[2]):
        fail("synthetic_data_required")
    website = fields.get("website", "")
    if website:
        match = re.fullmatch(r"https?://([^/:?#\s]+)(?::\d{1,5})?(?:[/?#]\S*)?", website, flags=re.IGNORECASE)
        if match is None or not reserved_host(match.group(1)):
            fail("synthetic_data_required")
    phone = fields.get("phone", "")
    if phone:
        digits = re.sub(r"[\s().+-]", "", phone)
        fictional = re.fullmatch(r"(?:1?\d{3})?55501\d\d", digits) is not None
        if not fictional and re.fullmatch(r"(?i)TEST-[0-9-]{1,24}", phone) is None:
            fail("synthetic_data_required")
    return fields
