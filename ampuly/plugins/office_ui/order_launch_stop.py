"""Resolve one exact in-flight launch and request a real Stop through its host.

The public WS layer supplies an owner-bound socket, not a token. The source
reader is read-only; the Stop host owns its write transaction, current-owner
checks, commit, close and receipt. No latest-by-order fallback and no runtime
termination claim.
"""
from __future__ import annotations

import contextlib
import sqlite3

from .launch_stop_intent import LaunchStopIntentError
from .launch_stop_socket import request_socket_launch_stop
from .launch_stop_source import LaunchStopSourceHeaders, read_launch_stop_source_headers


def _project(project_id: str) -> str:
    value = str(project_id or "").strip()
    if not value or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise LaunchStopIntentError("invalid_source")
    return value


def _order(order_id: str) -> str:
    value = str(order_id or "").strip()
    if not value or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise LaunchStopIntentError("invalid_source")
    return value


def resolve_launch_source(business, *, project_id: str, order_id: str,
                          outbox_id: str = "") -> LaunchStopSourceHeaders:
    """Build headers for one unambiguous active launch source.

    A caller may name the outbox. Otherwise the order must have exactly one
    pending/inflight/outcome_unknown launch row: choosing a "latest" row would
    stop a newer generation the person never saw.
    """
    project, order = _project(project_id), _order(order_id)
    path = getattr(business, "path", None)
    if not path:
        raise LaunchStopIntentError("invalid_source")
    with contextlib.closing(sqlite3.connect(str(path), timeout=0.0)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        try:
            if outbox_id:
                rows = conn.execute(
                    """SELECT outbox_id, status FROM business_outbox
                       WHERE outbox_id=? AND aggregate_id=? AND event_type='workflow_launch_requested'
                         AND status IN ('pending','inflight','outcome_unknown') LIMIT 2""",
                    (str(outbox_id), order)).fetchall()
            else:
                rows = conn.execute(
                    """SELECT outbox_id, status FROM business_outbox
                       WHERE aggregate_id=? AND event_type='workflow_launch_requested'
                         AND status IN ('pending','inflight','outcome_unknown')
                       ORDER BY sequence DESC LIMIT 2""",
                    (order,)).fetchall()
            if len(rows) != 1:
                raise LaunchStopIntentError("invalid_source")
            selected = str(rows[0][0] or "").strip()
            if not selected or len(selected) > 512:
                raise LaunchStopIntentError("invalid_source")
            source = read_launch_stop_source_headers(
                conn, project_id=project, order_id=order, outbox_id=selected)
            if type(source) is not LaunchStopSourceHeaders:
                raise LaunchStopIntentError("invalid_source")
            return source
        finally:
            conn.rollback()


def request_order_launch_stop(manager, ws, expected_binding, business, *,
                              project_id: str, order_id: str,
                              reason: str = "", outbox_id: str = ""):
    """Public adapter: exact source resolution plus socket-bound owned Stop."""
    source = resolve_launch_source(
        business, project_id=project_id, order_id=order_id, outbox_id=outbox_id)
    return request_socket_launch_stop(
        manager, ws, expected_binding=expected_binding,
        business_store=business, source=source,
        reason=str(reason or "Owner stopped the launch").strip() or "Owner stopped the launch",
    )
