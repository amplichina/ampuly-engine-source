"""The console's orders as the kernel's order port (``ampuly.core.order_liveness``, runtime contract).

The console builds its engines with this adapter (``server.create_app`` before ``initialize``, the Office service
factory, the CLI); the kernel asks it whether the order a card or a run belongs to has ended, and never opens the
business store itself.

It reads ``business.db`` under one home and never makes one: without it every order is live, as before the port. An
order is found by its id, else by the task it launched; when the engine names its project, only an order of that
project is found. It fails open: an order that cannot be read is live, so a card goes out rather than disappear.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from ampuly.core.order_liveness import ORDER_CANCELLED, ORDER_CLOSED, OrderKey


def order_ended_as(business: Any, order: Any) -> str:
    """How an order that has ended ended: ``cancelled`` (cancelled, or rejected before it ran) or ``closed``; "" when live.

    A cancel records its outcome on the closing event (``order_cancel.cancel_order``); a close after delivery does not.
    """
    from ampuly.plugins.office_ui.business_store import ENDED_ORDER_STATUSES

    status = str(getattr(order, "status", "") or "")
    if order is None or status not in ENDED_ORDER_STATUSES:
        return ""
    if status != "closed":
        return ORDER_CANCELLED
    events = list(business.events(str(getattr(order, "order_id", "") or "")) or [])
    closing = next((event for event in reversed(events) if event.get("kind") == status), None)
    note = closing.get("note") if isinstance(closing, dict) else None
    return ORDER_CANCELLED if isinstance(note, dict) and "cancel" in note else ORDER_CLOSED


class BusinessOrderLiveness:
    """``OrderLiveness`` over the console's business store under ``ampuly_home``."""

    def __init__(self, ampuly_home: Path | str) -> None:
        self.home = Path(ampuly_home)
        self._business: Any = None

    def _store(self) -> Any:
        from ampuly.plugins.office_ui.business_store import BusinessStore

        if self._business is None:
            self._business = BusinessStore(self.home)
        return self._business

    def _read(self, key: OrderKey, project_id: str | None) -> str:
        business = self._store()
        scope = (str(project_id or "").strip() or None) if project_id is not None else None
        order = business.get_order(key.order_id, project_id=scope) if key.order_id else None
        for task_id in key.launched_task_ids:
            if order is not None:
                break
            order = business.order_for_launched_task(task_id, project_id=scope)
        return order_ended_as(business, order)

    async def order_end(self, key: OrderKey, *, project_id: str | None = None) -> str:
        if not key or not (self.home / "business.db").is_file():
            return ""
        try:
            # Off the loop: the business store waits up to five seconds for its lock.
            return await asyncio.to_thread(self._read, key, project_id)
        except Exception:
            logger.opt(exception=True).debug("order port: the order of {} could not be read; taken as live", key)
            return ""
