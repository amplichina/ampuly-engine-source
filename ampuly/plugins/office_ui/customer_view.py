"""What a customer may see of their own order, and nothing else.

Nothing customer-facing exists today: a customer cannot see whether their work
is moving or say whether they accept it without somebody inside reading the
console to them. This builds the one thing that has to exist first -- the
projection itself -- as a pure function over the order, its acceptance record
and its timeline.

It deliberately does not open a door. There is no endpoint, no token and no
listener here; the owner asks for this view and decides how it reaches the
customer. Adding an unauthenticated surface is a decision about exposure, not
a detail of a projection, and it is not this module's to make. Since S3-6A
(2026-09-27) a logged-in ``customer`` account reads this same projection of
its own orders on the console (``ws_handler.customer_orders_view``); that is
still not a door to the outside: the account exists only when the owner makes
one, and no link or mail leaves here.

The rule the tests hold: an entry is included because somebody decided a
customer should see it, never because it happened to be in the timeline.
Internal role ids, employee names, task and run ids, file paths, model costs
and anything belonging to another customer stay out, and a field that cannot
be shown is left out rather than blanked -- an empty box invites a guess.
"""

from __future__ import annotations

import re
from typing import Any

# The stages a customer recognises, in the order they happen. Anything else in
# the timeline is internal by default: a reader of this file should be able to
# see the entire vocabulary a customer is shown.
CUSTOMER_STAGES: dict[str, str] = {
    "order_created": "已收到需求",
    "order_launched": "已开工",
    "gate_passed": "内部检查通过",
    "delivery_ready": "交付物已准备好",
    "acceptance_defined": "验收标准已确认",
    "acceptance_submitted": "已提交验收",
    "acceptance_accepted": "已验收",
    "acceptance_rejected": "验收被打回",
    "order_closed": "已结束",
}

# Never shown, whatever they are called. This is a fence, not the whole rule:
# it catches names we thought to forbid, so on its own a field added later
# arrives pre-approved. Callers that know which fields the customer actually
# filled in pass them as ``visible_fields`` and get default-deny instead.
_NEVER_SHOWN = re.compile(
    r"(task_id|run_id|session|checkpoint|employee|role_id|worker|agent|"
    r"path|workspace|cost|token|budget|provider|model|internal)",
    re.IGNORECASE,
)

# The shipped VI intake declares this as a workspace-relative file reference,
# not customer-readable text. Its name has no generic path marker.
_INTERNAL_FILE_FIELDS = frozenset({"existing_logo"})


def _visible_stage(entry: dict[str, Any]) -> str:
    kind = str(entry.get("kind", "") or "").strip()
    if entry.get("source") == "order":
        # BusinessStore's persisted vocabulary differs from the presentation
        # labels. These aliases apply only to order events, never runtime or
        # gate events that happen to use the same short names.
        kind = {
            "created": "order_created",
            "launched": "order_launched",
            "acceptance.criteria_defined": "acceptance_defined",
            "acceptance.submitted": "acceptance_submitted",
            "acceptance.accepted": "acceptance_accepted",
            "acceptance.rejected": "acceptance_rejected",
        }.get(kind, kind)
    return CUSTOMER_STAGES.get(kind, "")


def customer_timeline(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The stages of this order a customer is shown, in time order.

    An entry earns its place by being one of the named stages. Everything else
    -- every runtime turn, every internal gate detail, every effect receipt --
    is left out, and the count of what was left out is reported rather than
    hidden, so nobody mistakes this for the whole history.
    """
    out: list[dict[str, Any]] = []
    for entry in entries or []:
        stage = _visible_stage(entry)
        if not stage:
            continue
        out.append({"at": str(entry.get("at", "") or ""), "stage": stage})
    return out


def _safe_fields(fields: dict[str, Any], visible: set[str] | None = None) -> dict[str, Any]:
    """The order's own intake fields, minus anything that names our insides.

    ``visible`` is the list of fields the customer themselves filled in, taken
    from the order's workflow package. Given it, nothing else can appear --
    a field the product adds later is absent until somebody declares it. The
    fence still runs on top, so a declared field with an internal-sounding name
    is refused rather than trusted.
    """
    safe: dict[str, Any] = {}
    for key, value in dict(fields or {}).items():
        name = str(key)
        if visible is not None and name not in visible:
            continue
        if _NEVER_SHOWN.search(name) or name.lower() in _INTERNAL_FILE_FIELDS:
            continue
        if isinstance(value, (dict, list)):
            # Nested structures are internal shapes more often than not, and a
            # customer-facing page should show sentences, not payloads.
            continue
        text = str(value or "").strip()
        if text:
            safe[name] = text
    return safe


def customer_acceptance(acceptance: dict[str, Any] | None) -> dict[str, Any]:
    """The acceptance record as a customer sees it: where the round stands, the criteria, the round number, when
    it was decided and the note. Never the evidence (runs, files, hashes), the history or who inside submitted it.
    The one block ``build_customer_view`` shows and ``customer_accept`` answers with (S3-6A)."""
    record = dict(acceptance or {})
    status = str(record.get("status", "") or "").strip()
    return {
        "status": status or "not_started",
        "criteria": [str(item) for item in list(record.get("criteria", []) or [])],
        "round_no": max(1, int(record.get("round_no", 1) or 1)),
        "decided_at": str(record.get("decided_at", "") or ""),
        "note": str(record.get("decision_note", "") or ""),
    }


# The columns of an order a customer's own list carries (S3-6A ``order_list`` for a customer account): a strict subset
# of the owner's row (``WorkOrder.to_payload``), so the page reads names that exist and nothing that names our insides
# -- no workflow, org, source task, reviewer, launched task, raw fields, handling or attention.
CUSTOMER_ORDER_ROW_KEYS = ("order_id", "customer_id", "status", "brand_name", "created_at", "updated_at")


def customer_order_row(order: dict[str, Any]) -> dict[str, Any]:
    """One row of a customer's own order list: which order, whose, where it stands and when. Nothing else."""
    return {key: str(order.get(key, "") or "") for key in CUSTOMER_ORDER_ROW_KEYS}


def build_customer_view(
    *,
    order: dict[str, Any],
    acceptance: dict[str, Any] | None = None,
    timeline: dict[str, Any] | None = None,
    deliverables: list[dict[str, Any]] | None = None,
    visible_fields: list[str] | set[str] | None = None,
) -> dict[str, Any]:
    """One order, as its own customer may see it.

    ``can_accept`` is the question the customer actually has: is it my turn?
    It is true only when acceptance has been submitted for their decision, so
    a page built from this cannot offer a button for work that is still
    running.
    """
    order_id = str(order.get("order_id", "") or "")
    customer_id = str(order.get("customer_id", "") or "")
    allowed = {str(name) for name in visible_fields} if visible_fields is not None else None
    record = dict(acceptance or {})
    status = str(record.get("status", "") or "").strip()
    entries = list((timeline or {}).get("entries", []) or [])
    shown = customer_timeline(entries)
    view: dict[str, Any] = {
        "order_id": order_id,
        "customer_id": customer_id,
        "status": str(order.get("status", "") or ""),
        "brand_name": str(order.get("brand_name", "") or ""),
        "fields": _safe_fields(order.get("fields", {}) or {}, allowed),
        # Which rule produced that dict. "declared" means the workflow package
        # named these fields; "fence" means nothing declared anything and only
        # the deny-list ran, which is the weaker of the two.
        "fields_rule": "declared" if allowed is not None else "fence",
        "stages": shown,
        # Said out loud: this is a summary, not the record. The full history
        # exists and is not what a customer is being shown.
        "stages_hidden": max(0, len(entries) - len(shown)),
        "acceptance": customer_acceptance(record),
        "can_accept": bool(customer_id.strip()) and status == "submitted",
        "deliverables": [
            {"name": str(item.get("name", "") or ""), "at": str(item.get("at", "") or "")}
            for item in list(deliverables or [])
            if str(item.get("name", "") or "").strip()
        ],
    }
    return view


def belongs_to(view: dict[str, Any], customer_id: str) -> bool:
    """Whether this view is the given customer's to see.

    A view with no customer belongs to nobody: an order that was never bound
    to a customer must not become visible to the first one who asks.

    Written before anything called it, so that a customer-facing door could
    not be built without this check. Since S3-6A the console's customer-role
    handlers (``customer_orders_view`` on each order they list by id,
    ``customer_accept`` before recording anything) stand behind it: an order
    is the account's only when its customer id is one of the account's.
    The owner-only preview still asks about an order, not on behalf of a
    customer, and has no customer id to check.
    """
    owner = str(view.get("customer_id", "") or "").strip()
    asked = str(customer_id or "").strip()
    return bool(owner) and owner == asked
