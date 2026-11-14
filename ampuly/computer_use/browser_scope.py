"""X08 / C01 in company mode: an order's browser scope is the scope of every task that runs it.

The master console writes a delegation (hosts, screenshots, downloads, lease,
who delegated and when) into the order's own metadata. In company mode the
work happens in work-item runtime tasks whose ``origin_task_id`` is that
order; they are created with their own metadata, so without this the
delegation never reached the browser tools that enforce it. Only a real
delegation record travels (``browser_delegated_by`` and at least one host);
a newer delegation on the task itself wins. Only ``browser_*`` keys travel, plus the order's
binding identity (``customer_id``, ``environment_id``) when the work item lacks it, so the
work-item's browser session is counted under the order's customer (X08 per-customer cap).
"""
from __future__ import annotations

from typing import Any

PREFIX = "browser_"

# The identity BrowserBinding places a session under. A company-mode work-item runtime task is
# created with its own metadata and carries neither, so its browser session resolved to an empty
# customer and slipped past the per-customer cap (X08). When a real order delegation is inherited,
# these travel from the order too -- never overwriting a value the child already holds.
BINDING_IDENTITY_KEYS = ("customer_id", "environment_id")


def delegated_scope(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """The browser scope in `metadata`, when it is an owner's delegation record; otherwise empty."""
    meta = dict(metadata or {})
    if not str(meta.get("browser_delegated_by", "") or "").strip():
        return {}
    if not [d for d in list(meta.get("browser_allowed_domains") or []) if str(d or "").strip()]:
        return {}
    return {key: value for key, value in meta.items() if key.startswith(PREFIX)}


def inherit_browser_scope(child: dict[str, Any] | None, order: dict[str, Any] | None) -> dict[str, Any]:
    """`child` with the order's delegation in place of its own browser keys, unless its own delegation
    is newer; either way it carries the order's binding identity (customer, environment) when the child
    lacks it, so the work-item's browser session counts under the order's customer (X08)."""
    merged = dict(child or {})
    scope = delegated_scope(order)
    if not scope:
        return merged
    order_meta = dict(order or {})
    for key in BINDING_IDENTITY_KEYS:
        if not str(merged.get(key, "") or "").strip():
            value = str(order_meta.get(key, "") or "").strip()
            if value:
                merged[key] = value
    own = delegated_scope(merged)
    if own and str(own.get("browser_delegated_at", "") or "") > str(scope.get("browser_delegated_at", "") or ""):
        return merged
    for key in [key for key in merged if key.startswith(PREFIX)]:
        merged.pop(key)
    merged.update(scope)
    return merged
