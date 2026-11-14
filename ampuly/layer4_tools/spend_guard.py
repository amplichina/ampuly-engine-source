"""Hold the budget before the paid call, and be honest about what happened.

The project grant removed the prompt before every image. Without something
holding the ceiling, that turns "you may use this tool" into "you may spend
without end" -- which is exactly the reading the budget exists to refuse.

The hard part is not the happy path. It is the call that fails *after* the
request left: the money may already be spent, so releasing the hold would
invite a second paid attempt for something that might have run. That case is
marked unknown and left for a person, and the budget stops taking new
reservations until they answer.

A run with no budget attached is unlimited by construction, which is the
behaviour that existed before this file. That is stated rather than hidden,
so nobody reads "no error" as "under a ceiling".
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Iterator

# Exception classes cannot establish whether a paid request was dispatched:
# a decoder can raise ValueError after the provider has already billed it.
# Callers may explicitly release only where they know no request was sent.


class SpendGuard:
    """One reservation, and the three ways it can end."""

    def __init__(self, business: Any, budget_id: str, reservation_id: str, kind: str) -> None:
        self.business = business
        self.budget_id = budget_id
        self.reservation_id = reservation_id
        self.kind = kind
        self._closed = False

    @property
    def active(self) -> bool:
        return bool(self.reservation_id) and not self._closed

    def settle(self, actual: float | None = None) -> None:
        if not self.active:
            return
        self.business.settle_reservation(self.reservation_id, actual=actual)
        self._closed = True

    def release(self, reason: str = "") -> None:
        if not self.active:
            return
        self.business.release_reservation(self.reservation_id, reason=reason)
        self._closed = True

    def unknown(self, reason: str) -> None:
        if not self.active:
            return
        self.business.mark_reservation_unknown(self.reservation_id, reason=reason)
        self._closed = True


class _NoBudget(SpendGuard):
    """A run with no budget: nothing is held, and nothing pretends to be."""

    def __init__(self) -> None:
        super().__init__(None, "", "", "")

    @property
    def active(self) -> bool:
        return False


def inherit_order_budget(child: dict[str, Any] | None, order: dict[str, Any] | None) -> dict[str, Any]:
    """``child`` metadata carrying its order's allowance, when the order has one and the child has none of its own.

    A budget is found through the task that makes the paid call. The order's own task carried it and the work items
    under it did not, so an order's allowance held nothing: every image and model call was made by a work item. The
    allowance travels with the scope it is checked against (order, customer, environment, home); a child that already
    has a budget of its own (a node trial, a managed turn) keeps it.

    Which order the work is for travels whether or not that order has an allowance. It used to arrive only together
    with a budget (or a browser delegation), so under a plain order no work item knew its order or its customer: its
    sessions were recorded under no order -- cancelling the order could not find them to interrupt -- and a child
    flow it invoked was created for no customer. Only what the task lacks is filled in -- and nothing at all on a
    task that spends from a budget of its own: that budget is checked against the task's scope, and an order id it
    never had would turn every one of its paid calls into a scope mismatch.
    """
    merged = dict(child or {})
    source = dict(order or {})
    if str(source.get("order_id", "") or "").strip() and not str(merged.get("budget_id", "") or "").strip():
        for key in ("order_id", "customer_id", "environment_id"):
            value = str(source.get(key, "") or "").strip()
            if value and not str(merged.get(key, "") or "").strip():
                merged[key] = value
    budget_id = str(source.get("budget_id", "") or "").strip()
    if not budget_id or str(merged.get("budget_id", "") or "").strip():
        return merged
    merged["budget_id"] = budget_id
    for key in ("ampuly_home", "order_id", "customer_id", "environment_id"):
        value = str(source.get(key, "") or "").strip()
        if value and not str(merged.get(key, "") or "").strip():
            merged[key] = value
    if str(source.get(ORDER_ALLOWANCE_KEY, "") or "") == ORDER_ALLOWANCE:
        merged[ORDER_ALLOWANCE_KEY] = ORDER_ALLOWANCE
    # A child order spends from its parent's allowance: the budget answers to the parent's order while the work is
    # the child's. That one fact has to reach the tasks that make the paid calls, or each of them is refused.
    answers_to = str(source.get(BUDGET_ORDER_KEY, "") or "").strip()
    if answers_to:
        merged[BUDGET_ORDER_KEY] = answers_to
    return merged


# The order's model plan, frozen when the order was claimed. It lives beside the allowance because it is the same
# kind of fact -- something decided once, at the top, that every task below has to run under -- and because the
# two helpers must sit together or they will drift apart the first time one of them grows a case.
MODEL_PLAN_KEY = "model_plan"


class PolicyConflict(RuntimeError):
    """A task already carries a different model plan. Write-once, like the workflow contract."""


def stamp_order_policy(metadata: dict[str, Any] | None, *, plan: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    """``metadata`` pinned to an order's model plan, and whether anything changed.

    Write-once by digest: a run that already carries a plan keeps it, and a *different* plan raises rather than
    overwrite. Half an order on one plan and half on another is worse than a refusal -- the record would say the
    order ran under a plan that it only half ran under.
    """
    merged = dict(metadata or {})
    wanted = dict(plan or {})
    if not wanted:
        return merged, False
    existing = merged.get(MODEL_PLAN_KEY)
    if isinstance(existing, dict) and existing:
        before = str(existing.get("plan_digest", "") or "")
        after = str(wanted.get("plan_digest", "") or "")
        if before and after and before != after:
            raise PolicyConflict(
                f"this run is already pinned to model plan {before[:8]}; refusing to re-pin it to {after[:8]}"
            )
        return merged, False
    merged[MODEL_PLAN_KEY] = wanted
    return merged, True


def inherit_order_policy(child: dict[str, Any] | None, order: dict[str, Any] | None) -> dict[str, Any]:
    """``child`` metadata carrying the plan of the run above it, when the child has none of its own.

    The same shape as ``inherit_order_budget``, and for the same reason: these are the tasks that actually call
    the models, and a plan read from the live settings at turn time is not the plan the order was placed under.
    """
    merged = dict(child or {})
    if isinstance(merged.get(MODEL_PLAN_KEY), dict) and merged.get(MODEL_PLAN_KEY):
        return merged
    found = dict(order or {}).get(MODEL_PLAN_KEY)
    if isinstance(found, dict) and found:
        merged[MODEL_PLAN_KEY] = dict(found)
    return merged


# How a task says that its budget is the allowance of the order it works on (as opposed to a node trial's or a
# managed turn's own budget). The mark is what lets a later grant or a close find every task it has to touch.
ORDER_ALLOWANCE_KEY = "budget_kind"
ORDER_ALLOWANCE = "order_allowance"
# The order a task's budget answers to, when that is not the task's own order: a child order (or a grandchild)
# spending from the allowance of the order that started it. Written by the server from the budget's own record when
# it hands the budget down; never taken from a request.
BUDGET_ORDER_KEY = "budget_order_id"


def stamp_order_allowance(metadata: dict[str, Any] | None, *, budget_id: str, ampuly_home: str, order_id: str,
                          customer_id: str = "", environment_id: str = "", replace: tuple[str, ...] = ()) -> tuple[dict[str, Any], bool]:
    """``metadata`` pinned to an order's allowance, and whether anything changed.

    A task that already spends from a budget keeps it -- unless that budget is one of ``replace`` (the order's
    earlier, now closed, allowances: left in place they would refuse every paid call as "budget is closed").
    """
    merged = dict(metadata or {})
    current = str(merged.get("budget_id", "") or "").strip()
    if current and current != budget_id and current not in replace:
        return merged, False
    before = dict(merged)
    merged.update({"budget_id": budget_id, "ampuly_home": ampuly_home, "order_id": order_id, ORDER_ALLOWANCE_KEY: ORDER_ALLOWANCE})
    # Its own order's allowance: whatever it said about spending from the order above it is no longer true, and left
    # in place would hold this budget to that other order and refuse every paid call.
    merged.pop(BUDGET_ORDER_KEY, None)
    merged.pop("parent_budget_id", None)
    if customer_id and not str(merged.get("customer_id", "") or "").strip():
        merged["customer_id"] = customer_id
    if environment_id and not str(merged.get("environment_id", "") or "").strip():
        merged["environment_id"] = environment_id
    return merged, merged != before


def is_order_work(metadata: dict[str, Any] | None) -> bool:
    """Whether a task works for an order: its own order id, the order its budget answers to, or an order's allowance.

    Order work never fails over to another agent or to native (model-routing phase 0, C11). A child work item carries
    these through ``inherit_order_budget``, so it counts as its order's work too.
    """
    meta = dict(metadata or {})
    return bool(str(meta.get("order_id", "") or "").strip() or str(meta.get(BUDGET_ORDER_KEY, "") or "").strip()
                or is_order_allowance(meta))


def is_order_allowance(metadata: dict[str, Any] | None) -> bool:
    """Whether the budget on this task is an order's allowance (its own order's, or the one above it)."""
    meta = dict(metadata or {})
    return bool(str(meta.get("budget_id", "") or "").strip()) and str(meta.get(ORDER_ALLOWANCE_KEY, "") or "") == ORDER_ALLOWANCE


def stamp_shared_allowance(metadata: dict[str, Any] | None, *, budget_id: str, ampuly_home: str, answers_to: str,
                           order_allowance: bool, customer_id: str = "", environment_id: str = "",
                           replace: tuple[str, ...] = ()) -> tuple[dict[str, Any], bool]:
    """``metadata`` of a child order's task pinned to the allowance of the order above it, and whether it changed.

    Unlike ``stamp_order_allowance`` the task keeps its own ``order_id`` -- cancelling, resuming and the business
    tools all find a task by the order it works for -- and says separately which order its budget answers to. A task
    that already spends from another budget keeps it (a child order with an allowance of its own is held by that),
    unless that budget is one of ``replace``.

    The budget's customer and environment are filled in where the task names none: a work item only ever learns them
    together with a budget, so a child that started while nothing was held has neither, and a budget it is then
    pinned to would refuse its every call as a scope mismatch. A task that names another customer or environment is
    left alone -- pinning it to a budget it can never pass is not governance.
    """
    merged = dict(metadata or {})
    current = str(merged.get("budget_id", "") or "").strip()
    if current and current != budget_id and current not in replace:
        return merged, False
    for name, wanted in (("customer_id", customer_id), ("environment_id", environment_id)):
        if wanted and str(merged.get(name, "") or "").strip() not in {"", wanted}:
            return merged, False
    before = dict(merged)
    merged.update({"budget_id": budget_id, "parent_budget_id": budget_id})
    if customer_id and not str(merged.get("customer_id", "") or "").strip():
        merged["customer_id"] = customer_id
    if environment_id and not str(merged.get("environment_id", "") or "").strip():
        merged["environment_id"] = environment_id
    if ampuly_home and not str(merged.get("ampuly_home", "") or "").strip():
        merged["ampuly_home"] = ampuly_home
    if answers_to:
        merged[BUDGET_ORDER_KEY] = answers_to
    if order_allowance:
        merged[ORDER_ALLOWANCE_KEY] = ORDER_ALLOWANCE
    return merged, merged != before


def clear_order_allowance(metadata: dict[str, Any] | None, budget_ids: tuple[str, ...]) -> tuple[dict[str, Any], bool]:
    """``metadata`` without the order allowance it carried, when that allowance is one of ``budget_ids``."""
    merged = dict(metadata or {})
    if str(merged.get("budget_id", "") or "").strip() not in budget_ids or str(merged.get(ORDER_ALLOWANCE_KEY, "") or "") != ORDER_ALLOWANCE:
        return merged, False
    merged.pop("budget_id", None)
    merged.pop(ORDER_ALLOWANCE_KEY, None)
    # a child order's task also said whose allowance it was; with the allowance gone that says nothing true
    merged.pop(BUDGET_ORDER_KEY, None)
    merged.pop("parent_budget_id", None)
    return merged, True


def budget_order_id(metadata: dict[str, Any] | None) -> str:
    """The order a task's budget is expected to belong to.

    Normally the task's own order. A task of a child order that spends from the allowance of the order above it says
    so (``budget_order_id``, written by the server from the budget's own record, and carried to every work item by
    ``inherit_order_budget``). Comparing the child's own order with that budget refused every paid call of the child.
    The older form -- a child run task naming its parent through the parent contract -- is still understood.
    """
    meta = dict(metadata or {})
    if str(meta.get("budget_id", "") or "").strip():
        answers_to = str(meta.get(BUDGET_ORDER_KEY, "") or "").strip()
        if answers_to:
            return answers_to
    shared = str(meta.get("parent_budget_id", "") or "").strip()
    if shared and shared == str(meta.get("budget_id", "") or "").strip():
        parent = meta.get("workflow_parent")
        parent_order = str((parent or {}).get("parent_order_id", "") or "").strip() if isinstance(parent, dict) else ""
        if parent_order:
            return parent_order
    return str(meta.get("order_id", "") or "").strip()


def budget_for_task(task: Any) -> tuple[Any, str]:
    """The budget this task spends from, or ``(None, "")`` when it has none."""
    metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    budget_id = str(metadata.get("budget_id", "") or "").strip()
    home = str(metadata.get("ampuly_home", "") or "").strip()
    if not budget_id:
        return None, ""
    from ampuly.plugins.office_ui.business_store import BusinessStore

    if not home:
        from ampuly.core.config import get_ampuly_home

        home = str(get_ampuly_home())
    business = BusinessStore(Path(home))
    budget = business.run_budget(budget_id)
    if budget is None:
        raise RuntimeError("Budget not found; paid request not started")
    nested = metadata.get("harness_scope", {})
    if not isinstance(nested, dict):
        nested = {}
    expected = {
        "project_id": str(getattr(task, "project_id", "") or "default").strip() or "default",
        "customer_id": str(metadata.get("customer_id", nested.get("customer_id", "")) or "").strip(),
        "environment_id": str(metadata.get("environment_id", nested.get("environment_id", "")) or "").strip(),
        "order_id": budget_order_id(metadata),
    }
    for field, wanted in expected.items():
        actual = str(budget.get(field, "") or "").strip()
        if actual != wanted:
            raise RuntimeError(
                f"Budget {field} scope mismatch; paid request not started"
            )
    if str(budget.get("lifecycle", "") or "") != "open":
        raise RuntimeError("Budget is closed; paid request not started")
    return business, budget_id


@contextlib.contextmanager
def spend_guard(task: Any, *, kind: str, amount: float, note: str = "", idempotency_key: str = "") -> Iterator[SpendGuard]:
    """Hold budget for one paid call; the caller says how it ended.

    On the way out:

    * the caller settled or released -> that stands;
    * a confirmed local pre-dispatch failure -> caller explicitly releases;
    * anything else -> unknown, because the money may already be gone.
    """
    from ampuly.llm.budget import paid_tool_stop
    from ampuly.plugins.office_ui.business_store import OrderTransitionError
    # The legacy paid-tool gate only counts units, not employee CNY spending.
    # Reject before ledger/transport until a trusted finite tool quote exists.
    from ampuly.layer3_agent.employee_runtime import current_employee_turn
    if current_employee_turn() is not None:
        raise paid_tool_stop(OrderTransitionError(
            "This paid tool cannot enforce the employee monetary ceiling; request not started"), resource=kind)

    # Nothing has been sent yet: a refusal here is the allowance saying no, not the tool failing. It is raised as a
    # budget stop so the turn ends for the owner instead of handing the model an error to work around.
    try:
        business, budget_id = budget_for_task(task)
    except RuntimeError as exc:
        raise paid_tool_stop(exc, resource=kind) from exc
    if business is None:
        yield _NoBudget()
        return

    try:
        budget = business.run_budget(budget_id)
        if not budget or "cost_cny" in dict(budget.get("limits", {}) or {}):
            raise OrderTransitionError("This paid tool has no reviewed monetary quote; request not started")
        reservation = business.reserve_budget(
            budget_id, kind=kind, amount=amount, actor="runtime",
            idempotency_key=idempotency_key, note=note,
        )
    except OrderTransitionError as exc:
        raise paid_tool_stop(exc, resource=kind) from exc
    guard = SpendGuard(business, budget_id, str(reservation["reservation_id"]), kind)
    try:
        yield guard
    except BaseException as exc:  # noqa: BLE001 - the point is to not guess
        guard.unknown(reason=f"dispatch outcome not established: {type(exc).__name__}")
        raise
    else:
        # A caller that forgot to say what happened has not proved the call
        # failed, so the hold stands as a charge rather than quietly vanishing.
        if guard.active:
            guard.settle()
