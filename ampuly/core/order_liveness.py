"""Whether the order a piece of work belongs to has ended -- one kernel port (runtime contract).

Orders are the console's (its business store); the kernel never reads them itself. An engine is handed an
``OrderLiveness`` when it is built (``AmpulyEngine(order_liveness=...)``, forwarded to its project delegates and custom
runtimes, and handed to its company executor) and asks it

* at the one place an owner card is published (``AmpulyEngine._save_execution_checkpoint``), and before the three store
  transactions that publish a card of their own (a work item gate, the final delivery hand-off, a gate's failure
  settlement): an ended order gets no card from any of them;
* when a restart looks at a run: a run whose order has ended is marked ended, never parked again.

The default knows no order and says none has ended, so an engine built without one -- the CLI board, a test, an engine
made with ``__new__`` -- behaves as it did before the port.

The one reading is ``order_end(key)``: "" while the order is live or cannot be told (the port fails open: the card goes
out as before), ``ORDER_CANCELLED`` or ``ORDER_CLOSED`` once it has ended. Which order a card or a task belongs to is
read here too, from the task (``order_key_for_task``), completed from the run's root task when the task only names it
(``order_key_of``), or from a run's tasks (``order_key_for_tasks``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from loguru import logger

# How an order ended, as the port says it. A cancelled order ends its runs as cancelled, a closed one as closed.
ORDER_CANCELLED = "cancelled"
ORDER_CLOSED = "closed"
ORDER_ENDS = frozenset({ORDER_CANCELLED, ORDER_CLOSED})


@dataclass(frozen=True)
class OrderKey:
    """What names an order: its id when a task carries it, and the tasks that may be the one it launched.

    ``launched_task_ids`` are candidates only: an order is found by the task it launched (its run's root task), and a
    work item's own task never matches one.
    """

    order_id: str = ""
    launched_task_ids: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.order_id or self.launched_task_ids)


class OrderLiveness(Protocol):
    """The kernel's one question about orders."""

    async def order_end(self, key: OrderKey, *, project_id: str | None = None) -> str:
        """"" while the order ``key`` names is live or cannot be told; ``ORDER_CANCELLED`` / ``ORDER_CLOSED`` once ended."""
        ...


class NeverEndedOrders:
    """The default port: it knows no order, so none has ended."""

    async def order_end(self, key: OrderKey, *, project_id: str | None = None) -> str:
        return ""


NEVER_ENDED = NeverEndedOrders()


def knows_orders(port: Any) -> bool:
    """Whether ``port`` can say anything at all (the default cannot: nothing needs to be looked up for it)."""
    return port is not None and not isinstance(port, NeverEndedOrders) and callable(getattr(port, "order_end", None))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _unique(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(text for text in (_text(value) for value in values) if text))


def order_key_for_task(task: Any, *, root_task: Any = None) -> OrderKey:
    """The order ``task`` works for: its own ``order_id`` (else its root task's), and the task it names as its run's
    origin, then itself, as the task the order may have launched."""
    metadata = dict(getattr(task, "metadata", {}) or {})
    root_metadata = dict(getattr(root_task, "metadata", {}) or {})
    return OrderKey(
        order_id=_text(metadata.get("order_id")) or _text(root_metadata.get("order_id")),
        launched_task_ids=_unique((metadata.get("origin_task_id"), getattr(task, "id", ""))),
    )


def order_key_for_tasks(tasks: Iterable[Any], *, launched_task_ids: Iterable[Any] = ()) -> OrderKey:
    """The order a run's tasks work for: the first ``order_id`` one carries; its origins, ``launched_task_ids`` (such as
    the run's chat anchor) and the tasks themselves as the task the order may have launched."""
    tasks = list(tasks or ())
    order_id = next(
        (
            _text(dict(getattr(task, "metadata", {}) or {}).get("order_id"))
            for task in tasks
            if _text(dict(getattr(task, "metadata", {}) or {}).get("order_id"))
        ),
        "",
    )
    return OrderKey(
        order_id=order_id,
        launched_task_ids=_unique(
            [
                *(dict(getattr(task, "metadata", {}) or {}).get("origin_task_id") for task in tasks),
                *list(launched_task_ids or ()),
                *(getattr(task, "id", "") for task in tasks),
            ]
        ),
    )


async def order_key_of(store: Any, task: Any) -> OrderKey:
    """``order_key_for_task``, with the run's root task read from ``store`` when ``task`` names it but no order id."""
    metadata = dict(getattr(task, "metadata", {}) or {})
    origin = _text(metadata.get("origin_task_id"))
    root_task = None
    get_task = getattr(store, "get_task", None)
    if not _text(metadata.get("order_id")) and origin and origin != _text(getattr(task, "id", "")) and callable(get_task):
        try:
            root_task = await get_task(origin)
        except Exception:
            logger.opt(exception=True).debug("order of task {}: its root task {} unreadable", getattr(task, "id", ""), origin)
    return order_key_for_task(task, root_task=root_task)


async def order_end(port: Any, key: OrderKey, *, project_id: str | None = None) -> str:
    """The port's answer, failing open: "" for a live order, an unknown one, or a port that could not answer."""
    if not key or not knows_orders(port):
        return ""
    try:
        answer = _text(await port.order_end(key, project_id=project_id))
    except Exception:
        logger.opt(exception=True).debug("order {} unreadable; taken as live", key)
        return ""
    return answer if answer in ORDER_ENDS else ""


async def order_end_of_task(port: Any, store: Any, task: Any, *, project_id: str | None = None) -> str:
    """How the order ``task`` works for ended ("" while live or unknown). Nothing is read for the default port."""
    if task is None or not knows_orders(port):
        return ""
    key = await order_key_of(store, task)
    scope = _text(getattr(task, "project_id", "")) or _text(project_id) or None
    return await order_end(port, key, project_id=scope)
