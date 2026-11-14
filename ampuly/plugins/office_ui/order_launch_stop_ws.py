"""Exact-launch Stop: the WS adapter kept out of the 1.4 MB handler.

The service and host own the transaction; this module only resolves the
owner-bound socket and turns its committed receipt into an ACK.
"""
from __future__ import annotations

import asyncio

from .launch_stop_intent import LaunchStopIntentError
from .order_launch_stop import request_order_launch_stop


async def handle_exact_launch_cancel(handler, ws, data, *, project_id: str, order_id: str) -> None:
    from .ws_handler import _socket_behind, _socket_principal_entry, _socket_principal_state

    try:
        _, manager, entries = _socket_principal_state(handler)
        entry = _socket_principal_entry(entries, _socket_behind(ws))
        if entry is None:
            await handler._send_ack(ws, ok=False, action="order_cancel", project_id=project_id,
                order_id=order_id, error="owner_binding_required",
                stop_recorded=False, runtime_suspended=False)
            return
        business = handler._accounts().business
        fact = await asyncio.to_thread(
            request_order_launch_stop, manager, ws, entry[1], business,
            project_id=project_id, order_id=order_id,
            reason=str(data.get("reason", "") or ""),
            outbox_id=str(data.get("outbox_id", "") or ""),
        )
    except LaunchStopIntentError as exc:
        await handler._send_ack(ws, ok=False, action="order_cancel", project_id=project_id,
            order_id=order_id, error=exc.code,
            stop_recorded="unknown" if exc.code == "outcome_unknown" else False,
            runtime_suspended=False)
        return
    except Exception:
        await handler._send_ack(ws, ok=False, action="order_cancel", project_id=project_id,
            order_id=order_id, error="exact_launch_stop_unavailable",
            stop_recorded=False, runtime_suspended=False)
        return
    await handler._send_ack(ws, ok=True, action="order_cancel", project_id=project_id,
        order_id=order_id, stop_fact_id=fact.stop_fact_id,
        outbox_id=fact.outbox_id, from_status=fact.from_order_status,
        to_status=fact.to_order_status, stop_recorded=True,
        runtime_stopped="unknown", effects="not_rolled_back")
