"""Per-request model-count budgets, shared by normal calls and streams.

Monetary calls require a host-bound reviewed tariff. Missing prices are
refused. The existing business store remains the only budget ledger.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
import uuid
from contextlib import aclosing, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any


BUDGET_STOP_ARTIFACT = "budget_stop"
# The message an owner's interrupt cancels a running turn with (model-routing phase 0, open point 9). A call already
# sent when the owner stopped it is counted as having happened (assumed), so it does not hold the order for a person.
OWNER_INTERRUPT = "ampuly-owner-interrupt"


def budget_stop_kind(exc: BaseException) -> str:
    """Why the budget stopped the call, for the person who has to act on it.

    ``exhausted`` -- the allowance ran out (grant more, or lift it); ``unavailable`` -- the budget is closed or held
    for verification (resolve the held call, or grant a new allowance); ``uncertain`` -- a paid call ended without a
    known result and the budget is now held (resolve it); ``refused`` -- anything else the gate said no to. The class
    alone cannot tell them apart: under a bound budget every failure of a model call whose outcome is not known becomes
    a ``ModelBudgetStop`` (a vendor's received "no" goes up as the vendor's own error instead, C9).
    """
    from ampuly.plugins.office_ui.business_store import BudgetExceeded, OrderTransitionError

    if isinstance(exc, RouteRefused):
        return "route_refused"
    if isinstance(exc, LedgerUnavailable):
        return "ledger_unavailable"
    if isinstance(exc, BudgetStopSurfaced):
        return exc.kind
    if isinstance(exc, LedgerFinalizeStop):
        return "uncertain"
    seen: set[int] = set()
    cursor: BaseException | None = exc
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        if isinstance(cursor, BudgetExceeded):
            return "exhausted"
        if isinstance(cursor, OrderTransitionError):
            return "unavailable"
        cursor = cursor.__cause__ or cursor.__context__
    text = str(exc).lower()
    if "uncertain" in text or "verification" in text or "terminal response" in text:
        return "uncertain"
    return "refused"


class ModelBudgetStop(RuntimeError):
    """A budget denial or uncertain paid result: never an automatic retry."""


class RouteRefused(ModelBudgetStop):
    """A model call refused before anything was sent (model-routing phase 0).

    A key that is not set, a model that is not in the catalogue, a route to another vendor that cannot be made safely:
    nothing left the machine, nothing was spent, so no reservation is written and nothing is marked unknown. A
    ``ModelBudgetStop`` because every place that must stop and ask a person instead of retrying, approving or passing
    already catches that class. ``code`` says which refusal; ``model`` the model that was asked for.
    """

    default_code = "route_refused"

    def __init__(self, message: str = "", *, code: str = "", model: str = "") -> None:
        super().__init__(message)
        self.code = str(code or self.default_code)
        self.model = str(model or "")


def route_refused_notice(exc: RouteRefused) -> str:
    """One sentence for a person: the call was refused before it was sent, and what to check."""
    from ampuly.i18n_runtime import is_zh

    code = str(getattr(exc, "code", "") or "route_refused")
    return (f"模型调用在发出前被拒绝（{code}）：没有发出请求，也没有花费。请到「配置 → 模型」检查密钥或所选模型。"
            if is_zh() else
            f"The model call was refused before it was sent ({code}); nothing was sent or spent. "
            "Check the key or the chosen model under Settings → Models.")


class LedgerFinalizeStop(ModelBudgetStop):
    """The paid call was made; the ledger could not record how it ended. Its cause is a ledger error, which must not
    be read as "refused before it was sent": the work happened, and a person verifies the charge."""


class CallNotCharged(Exception):
    """Raised inside a reservation by a caller that knows its request was not served: it never left this machine, or
    the service answered "no" without doing the work (a status a caller reads off a response rather than from a typed
    vendor error). The reservation is released and this goes up for the caller to handle outside the reservation."""


class LedgerUnavailable(ModelBudgetStop):
    """The budget ledger could not be read or written before the call (model-routing phase 0, C12): the database was
    busy past the local retries, or could not be opened. Nothing was sent and nothing was spent; the allowance itself
    said nothing -- so the card does not blame it."""


class BudgetStopSurfaced(ModelBudgetStop):
    """A budget stop carried out of a nested turn whose failed result would otherwise read as "no answer".

    An assessment, a gate judge or an approval review runs as a turn of its own. When the order's allowance refuses
    it, the turn ends as an ordinary failed result, and "the reviewer said nothing" used to be treated as "nothing to
    object to": the gate passed, the medium-risk action was auto-approved. ``kind`` is ``budget_stop_kind``.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = str(kind or "refused")


RESOURCE_MARK = "[budget:{resource}]"
_paid_tool_stop_classes: dict[type, type] = {}


def paid_tool_stop(exc: BaseException, *, resource: str) -> BaseException:
    """The allowance refusing a paid TOOL (an image, say), as a budget stop that is still the ledger's own error.

    It stays an instance of what the ledger raised (callers and tests that expect ``BudgetExceeded`` keep working)
    and becomes a ``BudgetStopSurfaced`` as well, so the tool registry can tell it from a tool that merely failed.
    The message names the resource: the card the owner gets, and the check before "continue", read it from there.
    """
    base = type(exc)
    if isinstance(exc, BudgetStopSurfaced):
        return exc
    cls = _paid_tool_stop_classes.get(base)
    if cls is None:
        cls = _paid_tool_stop_classes[base] = type("PaidToolBudgetStop", (BudgetStopSurfaced, base), {})
    kind = budget_stop_kind(exc)
    if kind == "refused" and "closed" in str(exc).lower():
        kind = "unavailable"
    return cls(kind, f"{RESOURCE_MARK.format(resource=resource)} {exc}")


def stop_in_tool_results(results: Any) -> BudgetStopSurfaced | None:
    """The first budget stop among a batch's tool results, as the exception that ends the turn -- or ``None``.

    A tool that fails hands the model an error to work around. A tool the order's allowance refused is not the
    model's to work around (skip the images? try another tool? ask again?): the owner decided the ceiling, so the
    owner is asked (policy).
    """
    for item in list(results or []):
        result = dict(item.get("result", {}) or {}) if isinstance(item, dict) else {}
        kind = str(result.get(BUDGET_STOP_ARTIFACT, "") or "").strip()
        if kind:
            return BudgetStopSurfaced(kind, str(result.get("error", "") or "the order's allowance refused a paid tool"))
    return None


@dataclass(frozen=True)
class ModelBudgetScope:
    home: str
    budget_id: str
    project_id: str
    customer_id: str
    environment_id: str
    order_id: str


_scope: ContextVar[ModelBudgetScope | None] = ContextVar("ampuly_model_budget", default=None)
_retry: ContextVar[bool] = ContextVar("ampuly_model_budget_retry", default=False)


@contextmanager
def model_retry_scope(retrying: bool = True):
    token = _retry.set(retrying)
    try:
        yield
    finally:
        _retry.reset(token)


def budget_is_bound() -> bool:
    return _scope.get() is not None


def _budget_order_id(metadata: dict) -> str:
    # One rule with the paid-tool guard: a child run on its parent's allowance answers to the parent's order.
    from ampuly.layer4_tools.spend_guard import budget_order_id

    return budget_order_id(metadata)


@contextmanager
def model_budget_scope(task: Any):
    metadata = dict(getattr(task, "metadata", {}) or {})
    budget_id = str(metadata.get("budget_id", "") or "")
    scope = None
    if budget_id:
        from ampuly.core.config import get_ampuly_home
        scope = ModelBudgetScope(
            home=str(metadata.get("ampuly_home", "") or get_ampuly_home()),
            budget_id=budget_id,
            project_id=str(getattr(task, "project_id", "default") or "default"),
            customer_id=str(metadata.get("customer_id", "") or ""),
            environment_id=str(metadata.get("environment_id", "") or ""),
            order_id=_budget_order_id(metadata),
        )
    token = _scope.set(scope)
    retry_token = _retry.set(False)
    try:
        yield
    finally:
        _retry.reset(retry_token)
        _scope.reset(token)


async def _finish_thread(operation, *args, **kwargs):
    """Finish an already-submitted ledger write even through repeated cancel."""
    worker = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
    return worker.result(), cancelled


async def _finish_ledger(operation, *args, **kwargs):
    # A busy ledger is asked again here too, not only for the reservation: the store's transitions are
    # compare-and-set and answer "already done" when asked twice, so asking again is safe.
    try:
        return await _finish_thread(_with_ledger_retry, operation, *args, **kwargs)
    except Exception as exc:
        raise LedgerFinalizeStop("Model budget ledger could not finalize; manual verification required") from exc


def _hold_budget(scope: ModelBudgetScope, reason: str) -> None:
    """Put the budget on hold directly, when marking the call itself unknown could not be written: an unknown outcome
    must hold the order even then. Best effort -- the caller already stops with the ledger's own error."""
    try:
        _with_ledger_retry(_ledger(scope.home).mark_budget_pending_verification, scope.budget_id, reason=reason)
    except Exception:  # noqa: BLE001
        pass


class _ReservationGroup:
    def __init__(self, guards):
        self.guards = guards

    def settle(self):
        for guard in self.guards:
            guard.settle()

    def release(self, reason=""):
        for guard in self.guards:
            guard.release(reason=reason)

    def unknown(self, reason):
        for guard in self.guards:
            guard.unknown(reason=reason)


# One ledger object per home: building one runs the whole schema script, and that used to happen on every model call.
_LEDGERS: dict[str, Any] = {}
_LEDGERS_LOCK = threading.Lock()
# "database is locked" before anything was sent is retried here, a few times: it is the local ledger, not the vendor,
# so asking again sends nothing twice.
_LEDGER_LOCK_RETRIES = 3
_LEDGER_LOCK_PAUSE_SECONDS = 0.25


def _ledger(home: str):
    from pathlib import Path
    from ampuly.plugins.office_ui.business_store import BusinessStore

    key = str(home)
    with _LEDGERS_LOCK:
        business = _LEDGERS.get(key)
        if business is None or not business.path.exists():
            business = BusinessStore(Path(home))
            _LEDGERS[key] = business
    return business


def _ledger_locked(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def _with_ledger_retry(operation, *args, **kwargs):
    for attempt in range(_LEDGER_LOCK_RETRIES + 1):
        try:
            return operation(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if not _ledger_locked(exc) or attempt >= _LEDGER_LOCK_RETRIES:
                raise
            time.sleep(_LEDGER_LOCK_PAUSE_SECONDS * (attempt + 1))
    raise AssertionError("unreachable")


def _ledger_outage(exc: BaseException) -> bool:
    """Whether a refusal before dispatch came from the ledger itself (a database error), not from the allowance."""
    from ampuly.plugins.office_ui.business_store import OrderTransitionError

    seen: set[int] = set()
    cursor: BaseException | None = exc
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        if isinstance(cursor, (OrderTransitionError, ModelBudgetStop)):
            return False
        if isinstance(cursor, (sqlite3.Error, OSError)):
            return True
        cursor = cursor.__cause__ or cursor.__context__
    return False


def _reserve(scope: ModelBudgetScope):
    from ampuly.layer4_tools.spend_guard import SpendGuard

    business = _with_ledger_retry(_ledger, scope.home)
    budget = _with_ledger_retry(business.run_budget, scope.budget_id)
    if not budget:
        raise ModelBudgetStop("Model budget not found; request not started")
    for key in ("project_id", "customer_id", "environment_id", "order_id"):
        if str(budget.get(key, "") or "") != getattr(scope, key):
            raise ModelBudgetStop("Model budget scope mismatch; request not started")
    if "cost_cny" in dict(budget.get("limits", {}) or {}):
        raise ModelBudgetStop("Model monetary ceiling requires priced reservations; request not started")
    guards = []
    try:
        for kind in (("model_call", "retry") if _retry.get() else ("model_call",)):
            # One key per reservation, kept across the retries: a retry after a write that committed but whose answer
            # was lost gets the same hold back instead of writing a second one that nothing would ever settle.
            key = f"llm-{uuid.uuid4().hex}-{kind}"
            reservation = _with_ledger_retry(business.reserve_budget, scope.budget_id, kind=kind, amount=1,
                                             actor="runtime", note="LLMProvider request", idempotency_key=key)
            guards.append(SpendGuard(business, scope.budget_id, reservation["reservation_id"], kind))
    except Exception:
        # Neither reservation licenses dispatch until every required cap fits.
        _with_ledger_retry(_ReservationGroup(guards).release, reason="model request denied before dispatch")
        raise
    return _ReservationGroup(guards)


# Whether the request inside the current reservation has been handed to the transport. A refusal raised before that
# (``RouteRefused``) releases the reservation instead of holding the budget as unknown: nothing was sent.
_dispatched: ContextVar[bool] = ContextVar("ampuly_model_request_dispatched", default=False)
_active_guard: ContextVar[Any] = ContextVar("ampuly_active_model_budget_guard", default=None)


def _reserve_selected(scope, target, prepared):
    business = _with_ledger_retry(_ledger, scope.home)
    budget = _with_ledger_retry(business.run_budget, scope.budget_id)
    from ampuly.layer3_agent.employee_runtime import current_employee_turn
    if current_employee_turn() is not None or budget and "cost_cny" in dict(budget.get("limits", {}) or {}):
        from ampuly.llm.priced_runtime import PricedRuntimeGroup
        return PricedRuntimeGroup(business, scope, target, prepared, _retry.get())
    return _reserve(scope)


def prepared_budget_call(original):
    guard = _active_guard.get()
    return getattr(guard, "prepared_call", original)


async def before_model_dispatch(prepared):
    """Final ledger claim after application policy and Stop checks."""
    guard = _active_guard.get()
    if getattr(guard, "money_priced", False):
        guard.validate_call(prepared)
        _, cancelled = await _finish_thread(guard.claim)
        if cancelled:
            raise asyncio.CancelledError
        guard.validate_call(prepared)


def observe_budget_usage(*, model, prompt_tokens, completion_tokens):
    guard = _active_guard.get()
    if getattr(guard, "money_priced", False):
        guard.observe(model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)


def mark_dispatched() -> None:
    """Called by the provider immediately before a request goes to the transport."""
    _dispatched.set(True)


@asynccontextmanager
async def request_budget(*, target=None, prepared=None):
    scope = _scope.get()
    if scope is None:
        from ampuly.layer3_agent.employee_runtime import current_employee_turn
        if current_employee_turn() is not None:
            raise ModelBudgetStop("Employee execution requires its original order budget")
        yield False
        return
    try:
        guard, cancelled = await _finish_thread(_reserve_selected, scope, target, prepared)
    except Exception as exc:
        if _ledger_outage(exc):
            raise LedgerUnavailable("Model budget ledger unavailable; request not started") from exc
        raise ModelBudgetStop("Model budget unavailable or exhausted; request not started") from exc
    if cancelled:
        await _finish_ledger(guard.release, reason="cancelled before model dispatch")
        raise asyncio.CancelledError
    # Each reservation starts undispatched; on the way out the flag is set (never reset with a token: a stream's
    # reservation may be closed from another task) to what an enclosing reservation had, or higher -- a nested
    # reservation refused before its own dispatch must not make an enclosing one that did dispatch look unsent.
    outer = _dispatched.get()
    outer_guard = _active_guard.get()
    _active_guard.set(guard)
    _dispatched.set(False)
    try:
        yield True
    except BaseException as exc:
        release_reason = ""
        if not _dispatched.get():
            release_reason = f"refused before model dispatch: {type(exc).__name__}"
        elif isinstance(exc, RouteRefused) and not _dispatched.get():
            # Refused before the transport was reached: the reservation is released, the budget stays as it was.
            release_reason = f"refused before dispatch: {exc.code}"
        elif isinstance(exc, (asyncio.CancelledError, GeneratorExit)) and not _dispatched.get():
            # Stopped before anything was sent: nothing is owed.
            release_reason = f"cancelled before model dispatch ({type(exc).__name__})"
        elif isinstance(exc, CallNotCharged):
            release_reason = f"not charged: {exc}"
        else:
            status = _received_status(exc)
            if status is not None:
                # The vendor answered "no" (a received 4xx, C9): the work was not done and nothing is owed, so the
                # reservation is released and the vendor's own error goes up -- the caller can compact an over-long
                # context, park on a quota or re-ask (as a counted retry). Anything else stays unknown and held.
                release_reason = f"rejected by vendor: {status}"
        if release_reason:
            try:
                _, cancelled = await _finish_ledger(guard.release, reason=release_reason)
            except LedgerFinalizeStop as ledger_exc:
                # The hold could not be given back: stop on the ledger, not on what the vendor said, so nothing
                # re-asks on top of a hold nobody will settle.
                raise LedgerUnavailable("Model budget ledger could not release the hold; nothing was spent") from ledger_exc
            if cancelled and not isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
                # A cancel that arrived while the hold was being given back is not lost to a re-ask.
                raise asyncio.CancelledError from exc
            raise
        if (isinstance(exc, asyncio.CancelledError) and OWNER_INTERRUPT in exc.args
                and not getattr(guard, "money_priced", False)):
            # The owner stopped a call that was already sent (open point 9): counted as having happened (assumed),
            # so the order is not held waiting for somebody to say what the owner already knows.
            await _finish_ledger(guard.settle)
            raise
        try:
            await _finish_ledger(guard.unknown, reason=f"model outcome uncertain: {type(exc).__name__}")
        except LedgerFinalizeStop:
            await asyncio.to_thread(_hold_budget, scope, f"model outcome uncertain: {type(exc).__name__}")
            raise
        if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
            raise
        raise ModelBudgetStop("Model result uncertain; budget held for verification, no automatic retry") from exc
    else:
        _, cancelled = await _finish_ledger(guard.settle)
        if cancelled:
            raise asyncio.CancelledError
    finally:
        _active_guard.set(outer_guard)
        _dispatched.set(outer or _dispatched.get())


def _received_status(exc: BaseException) -> int | None:
    from ampuly.llm.provider import received_rejection

    return received_rejection(exc)


def budgeted_call(method):
    @wraps(method)
    async def call(*args, **kwargs):
        prepared = args[1] if len(args) > 1 else kwargs.get("call_kwargs")
        # A decorated plain function has no first positional target; the budget
        # then binds only to the active scope (production methods always pass self).
        async with request_budget(target=args[0] if args else None, prepared=prepared) as bounded:
            if bounded:
                kwargs["num_retries"] = 0
                if len(args) > 1:
                    args = (args[0], prepared_budget_call(prepared), *args[2:])
                else:
                    kwargs["call_kwargs"] = prepared_budget_call(prepared)
            return await method(*args, **kwargs)
    return call


def budgeted_stream(method):
    @wraps(method)
    async def stream(*args, **kwargs):
        prepared = args[1] if len(args) > 1 else kwargs.get("call_kwargs")
        async with request_budget(target=args[0] if args else None, prepared=prepared) as bounded:
            if bounded:
                kwargs["num_retries"] = 0
                if len(args) > 1:
                    args = (args[0], prepared_budget_call(prepared), *args[2:])
                else:
                    kwargs["call_kwargs"] = prepared_budget_call(prepared)
            stopped = False
            async with aclosing(method(*args, **kwargs)) as events:
                async for event in events:
                    if bounded and event.event_type == "error":
                        status = event.payload.get("status")
                        if isinstance(status, int) and not isinstance(status, bool):
                            # A received rejection (C9): the provider raises its own error next, and the
                            # reservation is released for it above. The error goes up, not the event.
                            continue
                        raise ModelBudgetStop("Model stream reported an uncertain result")
                    stopped = stopped or event.event_type == "message_stop"
                    yield event
            if bounded and not stopped:
                raise ModelBudgetStop("Model stream ended without a terminal response")
    return stream
