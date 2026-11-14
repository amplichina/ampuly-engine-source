"""Which customer a memory entry belongs to, and where it is kept.

Two customers served out of one project shared one memory file. Nothing
malicious was needed for that to matter: the second customer's run reads what
the first customer's run wrote, and a brief sentence about one client's
pricing turns up while working for another.

This module owns two things and no more: the rule that turns a customer id
into a directory-safe key, and the name for entries that belong to no customer
at all. The second matters as much as the first -- an unattributed entry has
to land somewhere it can be found again, never in whichever customer happened
to be running next.
"""

from __future__ import annotations

import hashlib
import re

# Entries with no customer keep the path they have always had, so nothing that
# exists on disk today moves or needs migrating.
NO_CUSTOMER = ""

# Where a customer's own bucket lives, under the project that serves them.
CUSTOMER_DIRNAME = "customers"

_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def customer_key(customer_id: str) -> str:
    """A directory-safe key for one customer, stable across runs.

    A customer id is chosen by whoever created the customer, so it can carry
    a slash, a colon, a space or a language this filesystem spells
    differently. Anything unsafe is replaced, and an id that is only unsafe
    characters falls back to a digest of the original -- never to a shared
    name, which would put two customers back in one bucket.
    """
    raw = str(customer_id or "").strip()
    if not raw:
        return NO_CUSTOMER
    cleaned = _SAFE.sub("-", raw).strip("-.")
    if not cleaned or set(cleaned) <= {"-", "."}:
        return "c-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    # A long id is truncated with its own digest attached, so two ids sharing
    # a prefix cannot collapse into one bucket.
    if len(cleaned) > 48:
        return cleaned[:40] + "-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return cleaned


def customer_key_for_task(task: object) -> str:
    """The customer this task's memory belongs to, or the no-customer bucket."""
    metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    return customer_key(str(metadata.get("customer_id", "") or ""))


def describe(customer: str) -> str:
    """What to call this bucket in a heading or a log line."""
    return customer or "no customer"
