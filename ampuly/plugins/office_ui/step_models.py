"""A workflow step's own model (4.7 A16/A17): the console's side of ``LLMProvider.for_model``.

An AI step runs on the configured default model unless it names one (``nodes[].model``). The console's provider
catalogue (settings_service.LLM_PROVIDERS) is the only place that knows which models exist and which variable holds
each provider's key; the kernel is handed a resolver and never imports the catalogue. Only variable *names* cross --
never a value.

There is no project level yet: it needs storage of its own (a schema change). The order is the step's model, then the
global default.
"""

from __future__ import annotations

import functools
from typing import Any

from loguru import logger

from ampuly.plugins.office_ui.settings_service import LLM_PROVIDERS


def provider_of(model: str) -> dict[str, Any] | None:
    chosen = str(model or "").strip()
    return next((provider for provider in LLM_PROVIDERS if chosen in provider["models"]), None)


def model_known(model: str) -> bool:
    return provider_of(model) is not None


def model_key_env(model: str) -> str:
    """The variable holding the key of the provider behind ``model`` ("" when the catalogue does not list it)."""
    provider = provider_of(model)
    return str(provider.get("api_key_env", "") or "") if provider else ""


def plan_params_for(model: str) -> dict[str, Any] | None:
    """What the run's frozen plan supplies for this model, or ``None`` when no plan is in scope.

    A plan is in scope only inside one of the entry sites that enters it, which is every resolution made on an
    order's behalf and nothing outside them. Off those paths this answers ``None`` and the catalogue's check is
    exactly what it was.
    """
    from ampuly.llm.provider import current_model_plan
    from ampuly.plugins.office_ui.model_tiers import tier_of_model, tier_params

    plan = current_model_plan()
    if not plan:
        return None
    return tier_params(plan, tier_of_model(plan, model))


def checked_model_key_env(model: str, *, residency_getter: Any = None) -> str:
    """``model_key_env``, after the catalogue's check (model-routing phase 0).

    An unknown id still gives "" (the provider refuses it as "no key variable"); a listed id the catalogue will not let
    run -- retired, overseas and off by default, not routable, waiting for phase-1 parameters -- raises
    ``CatalogueRefused`` naming why and what replaces it. The default model is never asked (``for_model`` returns the
    provider itself), so a default the owner chose keeps running.

    An entry held for phase-1 parameters is offered the run's own plan: those parameters are exactly what it was
    waiting for, so supplying them lifts the hold for this call without editing the catalogue.

    ``residency_getter`` is the setting of the provider this call is made for (``bind_step_model_resolver`` binds
    each provider's resolver with its own); a bare call has none and reads the process fallback.
    """
    from ampuly.llm.provider import CatalogueRefused
    from ampuly.plugins.office_ui.model_catalogue import entry, refusal
    from ampuly.plugins.office_ui.model_residency import model_refusal

    # The catalogue first, then the residency. An overseas entry is off by default whatever the residency says,
    # so reporting 「your residency setting forbids it」 would name a reason that changing the setting would not
    # fix. The residency check still stands behind it, for an entry that is overseas *and* on offer, and for the
    # 不用于训练 half, which is the only thing it refuses among today's mainland entries.
    refused = refusal(model, params=plan_params_for(model))
    if refused is None:
        residency, optout = current_residency(residency_getter)
        blocked = model_refusal(entry(model), residency, optout)
        if blocked:
            raise CatalogueRefused(f"model {model!r} is not allowed here ({blocked})", code=blocked,
                                   model=str(model or ""))
        return model_key_env(model)
    if refused["code"] == "model_unknown":
        return ""
    successor = str(refused.get("successor", "") or "")
    raise CatalogueRefused(
        f"model {model!r} is not offered ({refused['code']})" + (f"; use {successor}" if successor else ""),
        code=refused["code"], model=str(model or ""),
    )


# Where a bare call reads the residency setting when no order's plan is in scope and no provider's own setting was
# handed in. Bound rather than imported, like every other setting this layer needs: the config object is the
# kernel's. Every provider's own resolver carries its own setting instead (``bind_step_model_resolver``), so this is
# only the fallback for a call with no provider behind it; it is set where a request's own engine binds at the point
# of use (a launch, a node trial) and by the CLI's one engine -- never by the console's lookups.
_RESIDENCY_GETTER: Any = None


def current_residency(getter: Any = None) -> tuple[str, dict[str, str]]:
    """``(residency, no_training_optout)`` for this call.

    Precedence, and each step is there for a reason:

    1. **The order's frozen plan**, when one is in scope -- so an order is judged by the setting it was launched
       under, not by whatever the setting says today.
    2. **The provider's own setting** (``getter``: the one its resolver was bound with), for everything that is not
       an order: 安安, console chat, chores, the builder's model picker, a flow-editor trial. Each provider reads its
       own, so two projects' engines in one process never read each other's.
    3. **The process fallback** (``_RESIDENCY_GETTER``), for a bare call with no provider behind it.
    4. **Nothing bound → 只用境内.** Failing open would leave the strictest setting unenforced on exactly the
       traffic that has no order behind it; and that is also why steps 2 and 3 exist rather than treating everything
       unknown as 只用境内, which would make 允许出境 unreadable for that same traffic.
    """
    from ampuly.llm.provider import current_model_plan

    plan = current_model_plan()
    if plan:
        return (str(plan.get("residency", "") or ""),
                {str(k): str(v) for k, v in dict(plan.get("no_training_optout") or {}).items()})
    reader = getter if callable(getter) else _RESIDENCY_GETTER
    if callable(reader):
        try:
            residency, optout = reader()
            return str(residency or ""), {str(k): str(v) for k, v in dict(optout or {}).items()}
        except Exception:  # noqa: BLE001 - an unreadable setting is not a reason to open the door
            logger.warning("step models: the residency setting could not be read; holding at 只用境内")
    return "cn_only", {}


def bind_step_model_resolver(llm: Any, *, process_default: bool = True) -> bool:
    """Let ``llm.for_model`` find a step model's key variable, through the catalogue's check. False when this provider
    cannot take it.

    The resolver bound is this provider's own: it reads the residency setting from this provider's config, so the
    last provider bound in the process does not decide for the others (a provider built from this one -- a step's
    sibling, an order's runtime -- inherits it, and with it this setting). ``process_default`` also makes this
    provider's setting the fallback of a bare call (``_RESIDENCY_GETTER``); the console's attach passes False.
    """
    global _RESIDENCY_GETTER

    bind = getattr(llm, "bind_model_key_resolver", None)
    if not callable(bind):
        return False

    def _setting() -> tuple[str, dict[str, str]]:
        config = getattr(llm, "config", None)
        return (str(getattr(config, "residency", "") or ""),
                dict(getattr(config, "no_training_optout", {}) or {}))

    def _plan_choice(plan: Any, task_type: Any) -> str:
        """Which model this plan chooses for this task type. Bound, so the kernel never imports the tier table."""
        from ampuly.plugins.office_ui.model_tiers import resolve_model

        answer = resolve_model(dict(plan or {}), kind=str(task_type or ""))
        if answer.get("refused"):
            from ampuly.llm.budget import RouteRefused

            raise RouteRefused(
                f"the tier {answer.get('tier', '')!r} this call asks for is not set in the model table",
                code=str(answer["refused"]),
            )
        return str(answer.get("model", "") or "")

    @functools.wraps(checked_model_key_env)
    def _checked_for_this_provider(model: str) -> str:
        return checked_model_key_env(model, residency_getter=_setting)

    if process_default:
        _RESIDENCY_GETTER = _setting
    bind_plan = getattr(llm, "bind_model_plan_resolver", None)
    if callable(bind_plan):
        bind_plan(_plan_choice)
    bind(_checked_for_this_provider)
    return True


def step_model(node: dict[str, Any] | None) -> str:
    """The model a step names ("" = the default)."""
    return str(dict(node or {}).get("model", "") or "").strip()


def resolve_step_model(node: dict[str, Any] | None, default_model: str) -> dict[str, str]:
    """Which model a step runs on, and why: its own, or the global default."""
    own = step_model(node)
    return {"model": own, "source": "node"} if own else {"model": str(default_model or ""), "source": "global"}
