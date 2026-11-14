"""LLM provider layer built on LiteLLM for unified model access."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import math
import os
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from ampuly.core.windows_ssl import sanitize_windows_sslkeylogfile

sanitize_windows_sslkeylogfile()

import litellm
from loguru import logger

from ampuly.core.attachment_content import attachment_suffix
from ampuly.core.attachment_store import AttachmentRef
from ampuly.core.config import LLMConfig
from ampuly.core.models import ModelCapabilitySet, RuntimeLLMEvent
from ampuly.llm.budget import (RouteRefused, budgeted_call, budgeted_stream, mark_dispatched,
                            before_model_dispatch, observe_budget_usage)

litellm.suppress_debug_info = True
litellm.drop_params = True

_MULTIMODAL_MODEL_HINTS = (
    "gpt-4.1",
    "gpt-4o",
    "gpt-4.5",
    "gpt-5",
    "o1",
    "o3",
    "o4",
    "claude-3",
    "claude-sonnet-4",
    "claude-opus-4",
    "gemini",
    "pixtral",
    "llava",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "internvl",
    "minicpm-v",
    "glm-4v",
)

_DOCUMENT_MODEL_HINTS = (
    "gpt-4.1",
    "gpt-4o",
    "gpt-4.5",
    "gpt-5",
    "claude-3",
    "claude-sonnet-4",
    "claude-opus-4",
    "gemini",
)

_VIDEO_MODEL_HINTS = (
    "gemini",
    "veo",
    "video",
)

_TOOL_PROTOCOL_ERROR_HINTS = (
    "no tool output found for function call",
    "no tool output found",
    "messages with role 'tool' must be a response to a preceding message with 'tool_calls'",
    "assistant message with tool_calls",
    "tool_calls must be followed by tool messages",
    "missing tool response",
    "missing tool output",
    "tool_call_id",
)


def _normalized_model_name(model: str) -> str:
    if "/" in model:
        return model.split("/", 1)[1].strip().lower()
    return model.strip().lower()


# Used when neither user config nor litellm can supply a window. Conservative
# enough for modern models so compaction still has a real denominator.
_CONTEXT_WINDOW_FALLBACK = 128_000
_context_window_fallback_warned: set[str] = set()
_max_tokens_clamp_warned: set[str] = set()


def _clamp_max_tokens(model: str, requested: int) -> int:
    """Cap the requested output tokens at the model's known output limit.

    Providers disagree on how to handle an oversized max_tokens: some clamp
    silently, others (e.g. DeepSeek) reject the request outright. Clamping
    here keeps a generous config default (32768) safe on small-cap models.
    Unknown models pass through unchanged.
    """
    try:
        info = litellm.get_model_info(model)
        cap = info.get("max_output_tokens") or info.get("max_tokens")
    except Exception:
        return requested
    if not cap or requested <= int(cap):
        return requested
    if model not in _max_tokens_clamp_warned:
        _max_tokens_clamp_warned.add(model)
        logger.info(
            "max_tokens {} exceeds output limit {} of model={}; clamping.",
            requested,
            cap,
            model,
        )
    return int(cap)


_CONTEXT_WINDOW_OVERRIDES: tuple[tuple[str, int], ...] = (
    ("gpt-5.4-pro", 1_050_000),
    ("gpt-5.4-mini", 400_000),
    ("gpt-5.4-nano", 400_000),
    ("gpt-5.4", 1_050_000),
    ("gpt-5-pro", 400_000),
    ("gpt-5", 400_000),
)


_POE_CONTEXT_WINDOW_OVERRIDES: tuple[tuple[str, int], ...] = (
    ("claude-sonnet-4.5", 64_000),
    ("claude-sonnet-4-5", 64_000),
)


def _context_window_override(model: str) -> int | None:
    normalized = _normalized_model_name(model)
    for prefix, window in _CONTEXT_WINDOW_OVERRIDES:
        if normalized == prefix or normalized.startswith(f"{prefix}-"):
            return window
    return None


def _poe_context_window_override(model: str) -> int | None:
    normalized = _normalized_model_name(model)
    for prefix, window in _POE_CONTEXT_WINDOW_OVERRIDES:
        if normalized == prefix or normalized.startswith(f"{prefix}-"):
            return window
    return None


def _is_official_openai_base(api_base: str | None) -> bool:
    normalized = str(api_base or "").strip()
    if not normalized:
        return True
    try:
        parsed = urlparse(normalized)
    except Exception:
        return False
    hostname = (parsed.hostname or "").strip().lower()
    return hostname in {"api.openai.com", "openai.com"}


def _is_poe_base(api_base: str | None) -> bool:
    normalized = str(api_base or "").strip()
    if not normalized:
        return False
    try:
        parsed = urlparse(normalized)
    except Exception:
        return False
    hostname = (parsed.hostname or "").strip().lower()
    return hostname == "api.poe.com"


def _looks_like_multimodal_model(model: str) -> bool:
    normalized = _normalized_model_name(model)
    if any(hint in normalized for hint in _MULTIMODAL_MODEL_HINTS):
        return True
    return normalized.endswith("-vl") or "-vl-" in normalized or "_vl" in normalized


def _looks_like_document_capable_model(model: str) -> bool:
    normalized = _normalized_model_name(model)
    return any(hint in normalized for hint in _DOCUMENT_MODEL_HINTS)


def _looks_like_video_capable_model(model: str) -> bool:
    normalized = _normalized_model_name(model)
    return any(hint in normalized for hint in _VIDEO_MODEL_HINTS)


# Fields litellm can answer for, and the key it answers under. Anything not
# here is not knowable from the model map and stays an assumption -- streaming
# and streaming tool calls are transport behaviour rather than model metadata,
# and "thinking" is not a mapped property at all.
_LOOKUP_FIELDS: dict[str, str] = {
    "supports_tool_calling": "supports_function_calling",
    "supports_multimodal": "supports_vision",
}

_ALWAYS_ASSUMED: tuple[str, ...] = (
    "supports_streaming",
    "supports_streaming_tool_calls",
    "supports_thinking",
    "supports_documents",
    "supports_video",
)


def _model_info(model: str) -> dict[str, Any] | None:
    """What litellm knows about this model, or None if it knows nothing.

    The None is the point. ``litellm.supports_function_calling`` answers
    False for a model it has never heard of, which is the same answer it
    gives for a model that genuinely cannot call tools -- so reading it
    directly turns "no idea" into "definitely not". ``get_model_info``
    raising is what tells the two apart, and it is the same discriminator
    the context-window lookup above already relies on.
    """
    try:
        info = litellm.get_model_info(model)
    except Exception:
        return None
    return dict(info or {}) or None


def _parse_tool_arguments(tool_name: str, arguments: Any) -> tuple[Any, str | None, str | None]:
    """Parse tool-call arguments and preserve failures for downstream recovery."""
    if not isinstance(arguments, str):
        return arguments, None, None

    raw = arguments
    try:
        parsed = json.loads(raw)
        return parsed, raw, None
    except json.JSONDecodeError as e:
        snippet = raw[:500].replace("\n", "\\n")
        error = f"Invalid tool arguments JSON for `{tool_name}`: {e.msg} at char {e.pos}"
        logger.warning(f"{error}. Raw snippet: {snippet}")
        return raw, raw, error


# The HTTP statuses of a "no" the vendor sent back without doing the work (model-routing phase 0, C9). A 408 is a
# timeout and a 5xx a failure on the vendor's side: the request may have been served, so neither is in the set.
RECEIVED_REJECTION_STATUSES = frozenset({400, 401, 403, 404, 413, 422, 429})


class ProviderRejected(RuntimeError):
    """A stream's error event that carried a vendor's received rejection, raised typed (model-routing phase 0, C9).

    The runtime reads ``status_code`` (and ``error_class``, the provider's own class name), not the text: the quota
    park, the overflow compaction and the re-ask rules turn on what the vendor answered.
    """

    def __init__(self, status: int, message: str = "", *, error_class: str = "") -> None:
        super().__init__(message)
        self.status_code = int(status)
        self.error_class = str(error_class or "")


def received_rejection(exc: BaseException | None) -> int | None:
    """The HTTP status of a "no" the vendor sent back, or None when the outcome of the request is not known.

    Judged by type and status, never by text: a status error with a response (the OpenAI SDK's ``APIStatusError``,
    which litellm's errors extend) or a ``ProviderRejected``, with a status in ``RECEIVED_REJECTION_STATUSES``. A
    timeout (408 included), a 5xx, a connection reset, a stream cut, anything without a status: None -- the request
    may have been served, and must not be treated as known or sent again.
    """
    if exc is None or isinstance(exc, TimeoutError):
        return None
    from openai import APIStatusError

    if not isinstance(exc, (APIStatusError, ProviderRejected)):
        return None
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool) and status in RECEIVED_REJECTION_STATUSES:
        return status
    return None


class ProviderQuotaExhaustedError(RuntimeError):
    """The provider rejected the request for quota/rate-limit reasons.

    Raised by the agent runtime instead of retrying in place: replaying the
    same payload against an exhausted quota can only fail, so the company
    dispatcher parks the work (returns the item to READY and backs off)
    rather than failing it terminally (OBS-6).
    """


# ``llm.request_retries`` above 0 is ignored (C10); said once per process, not on every call.
_request_retries_warned = False


_CREDENTIAL_SCOPE: ContextVar[tuple[str, str]] = ContextVar(
    "ampuly_model_credential_scope", default=("", ""),
)


# Which model each call inside a ``model_call_receipt`` block was sent to -- a receipt names the model actually
# called, not the one a label or a setting was meant to name (model-routing phase 0).
_CALL_RECEIPT: ContextVar[list[dict[str, str]] | None] = ContextVar("ampuly_model_call_receipt", default=None)


# The run's frozen model plan, and the order it belongs to -- model routing phase 1.
#
# A contextvar because the provider is the only place that can be told, and the fact it needs ("this turn belongs
# to order X, whose settings were frozen like this") is known at the turn, several layers up. Nothing enters this
# scope until E7 wires the turn runners; until then every reader sees an empty plan, which is the same answer as
# 「there is no plan」 and therefore today's behaviour.
# ``None`` and ``{}`` are different answers on purpose: 「there is no plan」 and 「there is a plan and it is
# empty」 lead to opposite behaviour under an order, and a single falsy value could not tell them apart.
_MODEL_PLAN: ContextVar[tuple[dict[str, Any] | None, str]] = ContextVar("ampuly_model_plan", default=(None, ""))


@contextmanager
def model_plan_scope(plan: dict[str, Any] | None, *, order_id: str = ""):
    """Run this block with ``plan`` as the answer to 「这一步该用哪个模型」, and ``order_id`` as whose it is.

    ``order_id`` is the order this call belongs to, or ``""`` when it belongs to none. The provider never sees a
    task's metadata, so this string is the only way it can tell 「not an order」 from 「an order that was never
    given a plan」 -- and those two must not behave the same.
    """
    token = _MODEL_PLAN.set((dict(plan) if plan is not None else None, str(order_id or "")))
    try:
        yield
    finally:
        _MODEL_PLAN.reset(token)


def current_model_plan() -> dict[str, Any] | None:
    """The plan in scope, or ``None`` when there is none."""
    return _MODEL_PLAN.get()[0]


def current_plan_order() -> str:
    """The order the plan in scope belongs to, or ``""``."""
    return _MODEL_PLAN.get()[1]


@contextmanager
def model_call_receipt():
    """Collect ``{"sent", "served"}`` for every chat call answered inside this block."""
    calls: list[dict[str, str]] = []
    token = _CALL_RECEIPT.set(calls)
    try:
        yield calls
    finally:
        _CALL_RECEIPT.reset(token)


async def _run_dispatch_check(check: Any) -> None:
    """The caller's last check before a request leaves (a managed route's scope). It runs inside the reservation; a
    refusal from it is a refusal before sending, so the reservation is released rather than held as unknown."""
    try:
        await check()
    except RouteRefused:
        raise
    except Exception as exc:
        raise RouteRefused(f"the dispatch check refused this call ({type(exc).__name__})",
                           code="dispatch_check_refused") from exc


class ProviderCredentialError(RouteRefused):
    """This call's customer has a key configured whose variable is empty.

    Raised rather than falling back, because the fallback is the bug: the
    machine default spends an account nobody chose and leaves no trace
    except an invoice. A refusal before sending (``RouteRefused``): no
    reservation, nothing unknown, and every caller that stops for a person on
    a budget stop stops here too instead of retrying or approving.
    """

    default_code = "credential_refused"


class CatalogueRefused(ProviderCredentialError):
    """The model catalogue says this model may not be newly chosen: retired, overseas and off by default, not routable
    by this engine yet, or waiting for phase-1 parameters (model-routing phase 0). A refusal before sending; existing
    ``except ProviderCredentialError`` sites keep catching it."""

    default_code = "catalogue_refused"


@contextmanager
def model_credential_scope(customer_id: str = "", environment_id: str = ""):
    """Whose account the model calls inside this block spend.

    A context variable rather than an argument because the call sites
    are deep inside a runtime that does not otherwise know or care who
    the customer is; threading it through every frame would put a
    business fact into a dozen signatures that have no business with it.

    Entered with no customer, it clears the scope: the enclosing block
    does not leak into work that is nobody's in particular.
    """
    token = _CREDENTIAL_SCOPE.set((str(customer_id or ""), str(environment_id or "")))
    try:
        yield
    finally:
        _CREDENTIAL_SCOPE.reset(token)


def current_credential_scope() -> tuple[str, str]:
    """The customer and environment whose account this call spends."""
    return _CREDENTIAL_SCOPE.get()


def _provider_prefix(model: str) -> str:
    """The provider part of a litellm model id ("deepseek/deepseek-chat" -> "deepseek"); "" when it has none."""
    text = str(model or "").strip()
    return text.split("/", 1)[0] if "/" in text else ""


class LLMProvider:
    """Unified LLM interface via LiteLLM supporting tool calls."""

    # Well-known provider API-key env vars that litellm reads directly when no
    # explicit api_key is passed. Used only by ``has_credentials()`` to avoid a
    # false "no credentials" verdict for env-based setups. Missing a provider
    # here just preserves the old behavior (a real LLM attempt), never a wrong
    # skip of a working key.
    _CREDENTIAL_ENV_VARS = (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "AZURE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "DEEPSEEK_API_KEY",
        "TOGETHERAI_API_KEY",
        "ARK_API_KEY",
    )

    def __init__(self, config: LLMConfig, ampuly_home: Path | None = None) -> None:
        self.config = config
        self.ampuly_home = ampuly_home
        self._total_tokens_in = 0
        self._total_tokens_out = 0
        self._total_cost = 0.0

        self._api_key = config.api_key or (
            os.environ.get(config.api_key_env) if config.api_key_env else None
        ) or self._env_from_user_registry(config.api_key_env) or None
        self._api_base = config.api_base or None
        # Bound by the layer that knows which customer owns which
        # variable. Unbound -- every existing installation and every
        # test -- means every call uses the key this process started
        # with, exactly as before.
        self._resolve_key_env: Any = None
        # Bound by the layer that knows the provider catalogue: which
        # variable holds the key of the provider behind a model. Unbound
        # means no step may run on a model other than the default.
        self._model_key_env: Any = None
        # Set by bind_model_plan_resolver: which model an order's frozen plan chooses. Unbound means no plan can
        # be read, which is the same as having none -- and is what every path outside an order sees.
        self._plan_resolver: Any = None
        # Set by use_key_env: this engine's calls run on a customer's own
        # variable, so another provider's model must not quietly fall back
        # to this machine's account.
        self._customer_key_env = ""
        self.refresh_server_text_tariff()

    def refresh_server_text_tariff(self) -> bool:
        """Rebind only this provider's reviewed server-config CNY tariff."""
        from ampuly.llm.server_tariff_binding import bind_server_text_tariff
        return bind_server_text_tariff(self)

    def _transport_kwargs(self) -> dict[str, Any]:
        """Hard per-request timeout, and no transport retries, for every LLM call.

        Without a timeout, a connection dropped mid-request (e.g. WinError 10054) leaves its turn waiting forever; the
        timeout turns that into a fast failure. A request that went out is never sent again on its own (model-routing
        phase 0, C10): it may have been served, and a second send pays for it twice. litellm leaves the OpenAI SDK's
        own two retries on when ``num_retries`` is missing, so every request carries an explicit 0. ``request_retries``
        still loads from an older yaml and is ignored, with one warning.
        """
        global _request_retries_warned
        kwargs: dict[str, Any] = {"num_retries": 0}
        timeout = float(getattr(self.config, "request_timeout", 0) or 0)
        if timeout > 0:
            kwargs["timeout"] = timeout
        retries = int(getattr(self.config, "request_retries", 0) or 0)
        if retries > 0 and not _request_retries_warned:
            _request_retries_warned = True
            logger.warning("llm.request_retries={} is ignored: a model request that went out is never re-sent "
                           "automatically", retries)
        return kwargs

    @staticmethod
    def _env_from_user_registry(name: str | None) -> str | None:
        """Windows fallback: read a user-level env var straight from the registry.

        ``setx`` writes HKCU\\Environment, but terminals that were already open
        never see the new value — a server started from such a shell would run
        without credentials even though the key is durably configured. Reading
        the registry (and exporting into ``os.environ`` so litellm's own env
        lookups also succeed) removes that footgun. No key ever touches a file.
        """
        if not name or os.name != "nt":
            return None
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg_key:
                value = str(winreg.QueryValueEx(reg_key, name)[0]).strip()
        except OSError:
            return None
        if value:
            os.environ.setdefault(name, value)
            return value
        return None

    def bind_credential_resolver(self, resolver: Any) -> None:
        """Ask, per call, which variable this call's customer uses.

        Bound rather than imported: which customer owns which variable
        is a business fact, and this layer must not learn to read the
        business store to find it out. The resolver is handed a customer
        and an environment and returns a variable *name*. No value ever
        crosses.
        """
        self._resolve_key_env = resolver if callable(resolver) else None

    def bind_model_plan_resolver(self, resolver: Any) -> None:
        """Ask which model a plan chooses for a task type.

        Bound rather than imported, like the key resolver and the customer resolver: the tier table is the
        console's, not this layer's. The resolver is handed ``(plan, task_type)`` and returns a model id, or
        ``""`` when the plan has nothing to say.
        """
        self._plan_resolver = resolver if callable(resolver) else None

    def _plan_model(self, task_type: str | None = None) -> str:
        """What the plan in scope chooses, or ``""``.

        Refuses outright when this call belongs to an order that was never given a plan. Falling through to the
        live settings there would be the quiet version of the bug this phase exists to fix: the order would run
        on whatever the settings happen to say at that moment, and its record would not say so.
        """
        plan, order_id = _MODEL_PLAN.get()
        if order_id and plan is None:
            raise RouteRefused("this order has no model plan; it was not pinned at launch",
                               code="no_model_plan")
        if plan is None or not callable(getattr(self, "_plan_resolver", None)):
            return ""
        try:
            return str(self._plan_resolver(plan, task_type) or "")
        except RouteRefused:
            raise
        except Exception:  # noqa: BLE001 - a resolver that cannot answer is not a reason to pick something else
            logger.opt(exception=True).warning("the model plan could not be read; falling back to the routing table")
            return ""

    def bind_model_key_resolver(self, resolver: Any) -> None:
        """Ask which variable holds the key of the provider behind a model.

        Bound rather than imported, like the customer resolver: the provider
        catalogue is the console's, not this layer's. The resolver is handed a
        model id and returns a variable *name* ("" when it knows none).
        """
        self._model_key_env = resolver if callable(resolver) else None

    def inherit_bindings(self, other: Any) -> None:
        """Take over another provider's resolvers (a runtime built for an order runs its own provider)."""
        if isinstance(other, LLMProvider):
            self._plan_resolver = getattr(other, "_plan_resolver", None)
            self._model_key_env = other._model_key_env
            self._resolve_key_env = other._resolve_key_env
            if other._customer_key_env and not self._customer_key_env:
                self._customer_key_env = other._customer_key_env
            self.refresh_server_text_tariff()

    def _tier_updates_for(self, params: dict[str, Any] | None) -> dict[str, Any]:
        """What a tier's own parameters change about the sibling's config. ``{}`` when there are none.

        Two keys, and both matter:

        * ``reasoning_effort`` -- the same-provider branch keeps the parent's, and ``chat`` forwards it. With
          thinking off in a tier, a project that had set an effort would silently keep spending it on every
          tier'd step; with thinking on, the parent's effort is what 「想一想」 means here.
        * ``context_window`` -- written **only** when the tier records a cap of its own. A tier that sets none
          keeps the sibling at 0 and is capped by the work-window clamp instead: writing the global cap in as a
          scalar override would put 128k ahead of the real window of a model whose window is smaller.
        """
        if not params:
            return {}
        # Thinking off means no effort is spent; thinking on means the parent's own effort is what it costs.
        updates: dict[str, Any] = {
            "reasoning_effort": self.config.reasoning_effort if params.get("thinking") else None,
        }
        cap = 0
        try:
            cap = int(params.get("window_cap", 0) or 0)
        except (TypeError, ValueError):
            cap = 0
        if cap > 0:
            updates["context_window"] = cap
        return updates

    def for_model(self, model: str, *, customer_id: str = "", environment_id: str = "",
                  params: dict[str, Any] | None = None) -> "LLMProvider":
        """This provider running ``model`` -- a workflow step's own model -- with that provider's own key.

        ``params`` is the tier's own settings for this model (E3). Without them nothing here changes.

        The default model (or none) is this provider itself. A model of the same
        provider keeps this provider's key and settings. A model of another
        provider reads *its* key variable, and leaves the default provider's
        proxy base and reasoning effort behind: carrying this provider's key or
        base to another vendor is the one thing this must never do. A key that
        is not set stops the step before any call. A customer who pays with a
        key of their own is not moved onto this machine's account for another
        provider's model: that call is refused.
        """
        chosen = str(model or "").strip()
        if not chosen or chosen == self.config.default_model:
            return self
        resolver = self._model_key_env
        key_env = str(resolver(chosen) or "").strip() if callable(resolver) else ""
        if callable(resolver) and not key_env:
            raise ProviderCredentialError(
                f"no key variable is known for model {chosen!r}; the step cannot run on it"
            )
        # Two ids are one provider only when both name it: an unprefixed id says nothing about its vendor.
        mine, theirs = _provider_prefix(chosen), _provider_prefix(self.config.default_model)
        same_provider = bool(mine) and mine == theirs
        # The window belongs to the model: a scalar set for the default is not the chosen model's (0 = look it up).
        if same_provider:
            update = {"default_model": chosen, "routing": {}, "context_window": 0}
            update.update(self._tier_updates_for(params))
            sibling = LLMProvider(self.config.model_copy(update=update, deep=True), ampuly_home=self.ampuly_home)
            sibling._api_key = self._api_key
            sibling._api_base = self._api_base
            sibling._resolve_key_env = self._resolve_key_env
            sibling._model_key_env = self._model_key_env
            sibling._customer_key_env = self._customer_key_env
            sibling.refresh_server_text_tariff()
            return sibling
        if not key_env:
            raise ProviderCredentialError(
                f"no key variable is known for model {chosen!r}; the step cannot run on it"
            )
        # A customer who pays with a key of their own is not moved onto this machine's account for another
        # provider's model -- decided here, before any allowance is reserved, not at the call.
        if self._customer_key_env:
            raise ProviderCredentialError(
                f"this project's model calls authenticate with {self._customer_key_env}; a step on {chosen!r} "
                "(another provider) is refused rather than run on this machine's account"
            )
        parent_resolver = self._resolve_key_env
        customer = str(customer_id or "").strip()
        if parent_resolver is not None and customer:
            variable = str(parent_resolver(customer, str(environment_id or "")) or "").strip()
            if variable:
                raise ProviderCredentialError(
                    f"customer {customer!r} pays for models with {variable}; a step on {chosen!r} "
                    "(another provider) is refused rather than run on this machine's account"
                )
        update = {
            "default_model": chosen, "routing": {}, "api_key": "", "api_key_env": key_env,
            "api_base": "", "reasoning_effort": None, "context_window": 0,
        }
        # A cap the tier set applies whichever vendor serves the model; the effort does not cross vendors, and
        # the line above already cleared it.
        update.update({k: v for k, v in self._tier_updates_for(params).items() if k == "context_window"})
        sibling = LLMProvider(self.config.model_copy(update=update, deep=True), ampuly_home=self.ampuly_home)
        if not sibling._api_key:
            raise ProviderCredentialError(
                f"model {chosen!r} authenticates with {key_env}, which is not set; "
                "refusing to run the step on another account"
            )
        sibling._model_key_env = resolver
        if parent_resolver is not None:
            def _refuse_customer_key(customer_id: str, environment_id: str = "") -> str:
                variable = str(parent_resolver(customer_id, environment_id) or "").strip()
                if variable:
                    raise ProviderCredentialError(
                        f"customer {customer_id!r} pays for models with {variable}; a step on {chosen!r} "
                        "(another provider) is refused rather than run on this machine's account"
                    )
                return ""
            sibling._resolve_key_env = _refuse_customer_key
        return sibling

    def for_employee_model(self, model: str, *, customer_id: str = "", environment_id: str = "",
                           params: dict[str, Any] | None = None) -> "LLMProvider":
        """Server-only fixed employee route; no task plan/tier can redirect it.

        for_model retains the existing catalogue and cross-account refusal.
        Even the default model gets a detached provider: clearing routing on
        the live shared provider would change unrelated orders. Pricing is
        freshly bound from server config for this exact model/account.
        """
        chosen = str(model or "").strip()
        if not chosen:
            raise RouteRefused("Employee model must be explicit")
        target = self.for_model(chosen, customer_id=customer_id,
                                environment_id=environment_id, params=params)
        updates = dict(default_model=chosen, routing={}, tiers={}, task_tiers={},
                       tier_params={}, fallback={}, max_tokens=min(target.config.max_tokens, 3500))
        updates.update(target._tier_updates_for(params))
        sibling = LLMProvider(target.config.model_copy(update=updates, deep=True), ampuly_home=target.ampuly_home)
        sibling._api_key, sibling._api_base = target._api_key, target._api_base
        sibling._resolve_key_env = target._resolve_key_env
        sibling._model_key_env = target._model_key_env
        sibling._customer_key_env = target._customer_key_env
        sibling._plan_resolver = target._plan_resolver
        sibling._employee_pinned_model = chosen
        if not sibling.refresh_server_text_tariff():
            from ampuly.llm.priced_runtime import inherit_matching_model_pricing
            inherit_matching_model_pricing(target, sibling)
        return sibling

    def _check_employee_dispatch_model(self, prepared: dict[str, Any]) -> None:
        from ampuly.layer3_agent.employee_runtime import current_employee_turn
        turn = current_employee_turn()
        pinned = getattr(self, "_employee_pinned_model", "")
        expected = turn.model if turn is not None else pinned
        if expected and (pinned != expected or prepared.get("model") != expected
                         or self.config.default_model != expected):
            raise RouteRefused("The employee's frozen model does not match the final provider route")
        if expected and (type(prepared.get("max_tokens")) is not int
                         or not 0 < prepared["max_tokens"] <= min(self.config.max_tokens, 3500)):
            raise RouteRefused("The employee's output bound does not match its tested provider profile")

    def model_listed(self, model: str) -> bool | None:
        """Whether the model catalogue lists ``model``; ``None`` when no catalogue is bound (callers treat it as no).

        The default model is always this provider's own. Used where a model name comes from outside the owner's
        settings -- a subagent's override -- before anything is built on it (model-routing phase 0).
        """
        chosen = str(model or "").strip()
        if chosen and chosen == self.config.default_model:
            return True
        resolver = self._model_key_env
        if not callable(resolver):
            return None
        try:
            return bool(str(resolver(chosen) or "").strip())
        except Exception:  # noqa: BLE001 - a catalogue that refuses the id does not list it
            return False

    def _call_api_key(self) -> str | None:
        """The key for *this* call, which is not always the process's key.

        No customer in scope, or no resolver bound, answers with what the
        process started with -- the behaviour every installation has
        today. A customer whose variable is named but empty stops the
        call by name instead of quietly spending somebody else's account.
        """
        customer_id, environment_id = current_credential_scope()
        resolver = self._resolve_key_env
        if not customer_id or resolver is None:
            return self._api_key
        try:
            variable = str(resolver(customer_id, environment_id) or "").strip()
        except ProviderCredentialError:
            raise
        except Exception as exc:
            raise ProviderCredentialError(
                f"could not decide which key customer {customer_id!r} uses: "
                f"{type(exc).__name__}"
            ) from exc
        if not variable:
            # The customer has no key of their own; the process default
            # is the deliberate answer rather than an accident.
            return self._api_key
        value = (
            str(os.environ.get(variable, "") or "").strip()
            or self._env_from_user_registry(variable)
            or ""
        )
        if not value:
            raise ProviderCredentialError(
                f"customer {customer_id!r} authenticates with {variable}, which is not set; "
                "refusing to spend the account this process started with"
            )
        return value

    def use_key_env(self, name: str) -> bool:
        """Read this project's key from *this* variable from now on.

        One machine, several customers: without this, every run authenticates
        with whichever key the process started with, and "whose account paid
        for that" has no answer. The business layer knows which variable
        belongs to which customer; this is where that decision reaches the
        call.

        Only the *name* crosses the boundary — the value is read here, from
        the same environment mechanism as before, and is never returned,
        logged or stored. An empty or unset name changes nothing and says so,
        so a caller cannot mistake "did not apply" for "applied".
        """
        variable = str(name or "").strip()
        if not variable:
            return False
        value = str(os.environ.get(variable, "") or "").strip() or self._env_from_user_registry(variable) or ""
        if not value:
            return False
        self.config.api_key_env = variable
        self._api_key = value
        self._customer_key_env = variable
        self.refresh_server_text_tariff()
        return True

    def has_credentials(self) -> bool:
        """Whether an LLM call can plausibly authenticate.

        True when a key is configured (``api_key`` / ``api_key_env``) or a
        well-known provider env var is present. False only when no credential
        is found anywhere — callers use that to skip LLM work that would
        certainly fail (e.g. native agent selection when an external agent can
        run the task instead). A False at worst degrades to rule-based behavior,
        which stays functional; it never blocks execution.
        """
        if self._api_key:
            return True
        return any(os.environ.get(var) for var in self._CREDENTIAL_ENV_VARS)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "tokens_in": self._total_tokens_in,
            "tokens_out": self._total_tokens_out,
            "estimated_cost": self._total_cost,
        }

    def _select_model(self, task_type: str | None = None) -> str:
        """Which model this task type runs on.

        The chain is most-specific-first: one of the six kinds tries its own slot and then the shared
        ``quick_tasks`` one; anything else tries only its own name; no task type at all tries nothing and takes
        the default. With ``routing`` empty, or holding only ``quick_tasks``, that is byte-for-byte the answer
        this function gave before the chain existed.
        """
        from ampuly.llm.task_kinds import routing_chain

        pinned = getattr(self, "_employee_pinned_model", "")
        if pinned:
            return pinned
        chosen = self._plan_model(task_type)
        if chosen:
            return chosen
        for key in routing_chain(task_type):
            if key in self.config.routing:
                return self.config.routing[key]
        return self.config.default_model

    def _config_context_window_override(self, model: str) -> int | None:
        """User-configured context window for models litellm cannot map.

        Per-model overrides win over the scalar default. Both come from
        ``LLMConfig`` so proxy/self-hosted models (doubao, minimax, glm, …)
        can report a real context window to the usage ring and compaction.
        """
        per_model = getattr(self.config, "context_window_overrides", None) or {}
        normalized = _normalized_model_name(model)
        for key, window in per_model.items():
            candidate = _normalized_model_name(str(key))
            if candidate and (normalized == candidate or normalized.startswith(f"{candidate}-")):
                try:
                    value = int(window)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    return value
        try:
            scalar = int(getattr(self.config, "context_window", 0) or 0)
        except (TypeError, ValueError):
            scalar = 0
        return scalar if scalar > 0 else None

    def get_context_window(self, task_type: str | None = None, model: str | None = None) -> int | None:
        """The window this call may use, after the work cap.

        A wrapper rather than a clamp written at the end of the resolver below: that function returns from five
        places and binds no name holding the answer, so a clamp at the end would only ever reach the fallback
        branch -- which is already 128000, a no-op -- while the 1M-token and 131,072-token paths the cap exists
        for all return earlier. Wrapping caps every branch by construction.

        A tier's own cap arrives through the resolver's first branch, so ``min`` keeps the smaller of the two:
        a tier may ask for less than the work cap, never for more. ``work_window_cap = 0`` disables the cap.
        """
        resolved = self._uncapped_context_window(task_type, model)
        cap = int(getattr(self.config, "work_window_cap", 0) or 0)
        return min(resolved, cap) if cap > 0 and resolved else resolved

    def _uncapped_context_window(self, task_type: str | None = None, model: str | None = None) -> int | None:
        resolved_model = model or self._select_model(task_type)
        config_override = self._config_context_window_override(resolved_model)
        if config_override is not None:
            return config_override
        poe_override = _poe_context_window_override(resolved_model) if _is_poe_base(self._api_base) else None
        if poe_override is not None:
            return poe_override
        override = _context_window_override(resolved_model) if _is_official_openai_base(self._api_base) else None
        if override is not None:
            return override
        try:
            # max_input_tokens is the context window; litellm.get_max_tokens()
            # returns the "max_tokens" map entry, which for many models (e.g.
            # deepseek) is the OUTPUT cap and would wildly under-report here.
            info = litellm.get_model_info(resolved_model)
            limit = info.get("max_input_tokens") or info.get("max_tokens")
            if limit:
                return int(limit)
            reason = "model is not mapped in litellm"
        except Exception as e:
            reason = str(e)
        if resolved_model not in _context_window_fallback_warned:
            _context_window_fallback_warned.add(resolved_model)
            logger.warning(
                "Unable to resolve context window for model={} ({}); assuming {} tokens. "
                "Set llm.context_window or llm.context_window_overrides in llm_config.yaml "
                "to use the model's real window.",
                resolved_model,
                reason,
                _CONTEXT_WINDOW_FALLBACK,
            )
        return _CONTEXT_WINDOW_FALLBACK

    def count_input_tokens(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        task_type: str | None = None,
        model: str | None = None,
    ) -> int | None:
        resolved_model = model or self._select_model(task_type)
        try:
            return int(litellm.token_counter(
                model=resolved_model,
                messages=messages,
                tools=tools,
            ))
        except Exception as e:
            logger.warning(f"Unable to count prompt tokens for model={resolved_model}: {e}")
            return None

    def count_text_tokens(
        self,
        text: str,
        task_type: str | None = None,
        model: str | None = None,
    ) -> int | None:
        resolved_model = model or self._select_model(task_type)
        try:
            return int(litellm.token_counter(
                model=resolved_model,
                text=text,
            ))
        except Exception as e:
            logger.warning(f"Unable to count text tokens for model={resolved_model}: {e}")
            return None

    def get_capabilities(
        self,
        task_type: str | None = None,
        model: str | None = None,
    ) -> ModelCapabilitySet:
        resolved_model = model or self._select_model(task_type)
        normalized = _normalized_model_name(resolved_model)
        provider_family = resolved_model.split("/", 1)[0].strip().lower() if "/" in resolved_model else ""
        supports_thinking = any(hint in normalized for hint in ("o1", "o3", "o4", "gpt-5", "claude", "reason"))
        # The values the product acts on, unchanged. What follows adds the
        # evidence beside them rather than replacing them: a model the map
        # calls incapable may still work through a proxy, and flipping a flag
        # here would disable tools for a setup that runs today.
        assumptions: dict[str, bool] = {
            "supports_streaming": True,
            "supports_tool_calling": True,
            "supports_streaming_tool_calls": True,
            "supports_thinking": supports_thinking,
            "supports_multimodal": _looks_like_multimodal_model(resolved_model),
            "supports_documents": _looks_like_document_capable_model(resolved_model),
            "supports_video": _looks_like_video_capable_model(resolved_model),
        }
        info = _model_info(resolved_model)
        if info is None:
            # Unmapped -- a proxy or self-hosted model. Every field is a guess,
            # and saying so is the whole difference between a profile and
            # seven cheerful constants.
            assumed = tuple(assumptions)
            conflicts: tuple[str, ...] = ()
            known: dict[str, Any] = {}
        else:
            known = {
                field_name: bool(info.get(key))
                for field_name, key in _LOOKUP_FIELDS.items()
                if info.get(key) is not None
            }
            assumed = tuple(
                name for name in assumptions if name not in known
            )
            conflicts = tuple(
                name for name, value in known.items() if assumptions[name] != value
            )
        return ModelCapabilitySet(
            model=resolved_model,
            supports_streaming=assumptions["supports_streaming"],
            supports_tool_calling=assumptions["supports_tool_calling"],
            supports_streaming_tool_calls=assumptions["supports_streaming_tool_calls"],
            supports_thinking=assumptions["supports_thinking"],
            supports_multimodal=assumptions["supports_multimodal"],
            supports_documents=assumptions["supports_documents"],
            supports_video=assumptions["supports_video"],
            provider_family=provider_family,
            metadata={
                "api_base": self._api_base or "",
                "model_is_mapped": info is not None,
                "known": known,
            },
            assumed=assumed,
            conflicts=conflicts,
        )

    def build_cache_fingerprint(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        task_type: str | None = None,
        model: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        resolved_model = model or self._select_model(task_type)
        payload = {
            "model": resolved_model,
            "messages": messages,
            "tools": tools or [],
            "extra": extra or {},
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def is_rate_limit_error(self, error: Exception) -> bool:
        """Classify provider quota / rate-limit rejections.

        These never produce model output and never get better by replaying
        the identical payload, so callers must park/back off instead of
        burning conversation-feedback retries (OBS-6). Classification is by
        exception type when available and by error text otherwise — the
        streaming path re-raises provider errors as plain RuntimeError with
        only the message preserved.
        """
        if isinstance(error, litellm.exceptions.RateLimitError):
            return True
        if "ratelimit" in type(error).__name__.lower():
            return True
        if getattr(error, "status_code", None) == 429:
            return True
        message = str(error).lower()
        keywords = (
            "rate limit",
            "rate_limit",
            "ratelimit",
            "too many requests",
            "insufficient_quota",
            "quota exceeded",
            "exceeded your quota",
            "quota exhausted",
            "error code: 429",
            "status code: 429",
            "http 429",
            # Chinese-provider spellings of the same rejection (Volces/DeepSeek
            # and other domestic endpoints return localized error text).
            "请求过于频繁",
            "配额已用完",
            "配额耗尽",
            "触发限流",
        )
        return any(keyword in message for keyword in keywords)

    def is_context_overflow_error(self, error: Exception) -> bool:
        if isinstance(error, litellm.exceptions.ContextWindowExceededError):
            return True
        if isinstance(error, ProviderRejected) and error.error_class == "ContextWindowExceededError":
            return True
        message = str(error).lower()
        keywords = (
            "context window",
            "context length",
            "maximum context length",
            "prompt is too long",
            "too many tokens",
            "context_length_exceeded",
            "token limit exceeded",
        )
        return any(keyword in message for keyword in keywords)

    def is_tool_protocol_error(self, error: Exception) -> bool:
        message = str(error).lower()
        return any(hint in message for hint in _TOOL_PROTOCOL_ERROR_HINTS)

    @staticmethod
    def sanitize_tool_call_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop incomplete or stray assistant/tool tool-call transcripts."""
        sanitized: list[dict[str, Any]] = []
        pending_ids: list[str] = []
        buffered_block: list[dict[str, Any]] = []

        def _copy_message(message: dict[str, Any]) -> dict[str, Any]:
            cloned = dict(message)
            if isinstance(message.get("tool_calls"), list):
                cloned["tool_calls"] = [dict(item) for item in message["tool_calls"]]
            return cloned

        for message in messages:
            role = str(message.get("role", "") or "").strip()
            if not pending_ids:
                if role == "assistant" and isinstance(message.get("tool_calls"), list) and message["tool_calls"]:
                    ids = [
                        str(item.get("id", "") or "").strip()
                        for item in message["tool_calls"]
                        if isinstance(item, dict) and str(item.get("id", "") or "").strip()
                    ]
                    if not ids:
                        continue
                    buffered_block = [_copy_message(message)]
                    pending_ids = ids
                    continue
                if role == "tool":
                    continue
                sanitized.append(_copy_message(message))
                continue

            if role == "tool":
                tool_call_id = str(message.get("tool_call_id", "") or "").strip()
                if tool_call_id and tool_call_id in pending_ids:
                    buffered_block.append(_copy_message(message))
                    pending_ids = [item for item in pending_ids if item != tool_call_id]
                    if not pending_ids:
                        sanitized.extend(buffered_block)
                        buffered_block = []
                continue

            if role == "assistant" and isinstance(message.get("tool_calls"), list) and message["tool_calls"]:
                ids = [
                    str(item.get("id", "") or "").strip()
                    for item in message["tool_calls"]
                    if isinstance(item, dict) and str(item.get("id", "") or "").strip()
                ]
                buffered_block = [_copy_message(message)] if ids else []
                pending_ids = ids
                continue

            buffered_block = []
            pending_ids = []
            sanitized.append(_copy_message(message))

        return sanitized

    def prepare_user_message_content(
        self,
        content: str,
        *,
        attachment_refs: list[dict[str, Any]] | None = None,
        task_type: str | None = None,
    ) -> str | list[dict[str, Any]]:
        text = str(content or "")
        refs = list(attachment_refs or [])
        if not refs:
            return text

        model = self._select_model(task_type)
        parts = self._build_direct_attachment_parts(model, refs)
        if not parts:
            return text

        content_parts: list[dict[str, Any]] = []
        if text:
            content_parts.append({"type": "text", "text": text})
        content_parts.extend(parts)
        return content_parts

    async def _acompletion(self, **call_kwargs: Any) -> Any:
        """The one place a turn leaves this machine.

        Named so a scripted provider can replace the transport alone: every
        caller above still runs the production selection, parsing, usage
        accounting and event conversion, which is the part worth testing
        offline. Nothing else about this class knows the difference.
        """
        return await litellm.acompletion(**call_kwargs)

    # Call arguments that would choose the model, the account or the host behind the provider's back (routing phase 0).
    _ROUTE_ARGUMENTS = frozenset({"model", "api_key", "api_base", "base_url", "custom_llm_provider", "api_version"})

    def _prepare_call(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        task_type: str | None,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        """Everything decided before a request may be sent: the model, the key, the host, the parameters.

        Runs before the budget reservation (model-routing phase 0), so a refusal here -- a key that is not set, a
        routing argument a caller may not pass -- writes no reservation and holds nothing.
        """
        chosen = sorted(self._ROUTE_ARGUMENTS & set(extra))
        if chosen:
            raise RouteRefused(f"call arguments may not choose the model, key or host ({', '.join(chosen)})",
                               code="call_override_refused")
        model = self._select_model(task_type)
        temp = temperature if temperature is not None else self.config.temperature
        max_tok = _clamp_max_tokens(model, max_tokens if max_tokens is not None else self.config.max_tokens)

        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tok,
        }
        if stream:
            call_kwargs["stream"] = True
            # OpenAI-compatible providers (DeepSeek included) only report token
            # usage on a stream when asked; without this every streamed turn
            # was billed by the provider and counted as zero here.
            call_kwargs["stream_options"] = {"include_usage": True}
        call_kwargs.update(self._transport_kwargs())
        call_kwargs.update(extra)
        # A caller cannot turn transport retries back on (C10): no second send of a request that went out.
        call_kwargs["num_retries"] = 0
        call_kwargs.pop("max_retries", None)
        if self.config.reasoning_effort and "reasoning_effort" not in call_kwargs:
            call_kwargs["reasoning_effort"] = self.config.reasoning_effort
        if self._api_base:
            call_kwargs["api_base"] = self._api_base
        call_key = self._call_api_key()
        if call_key:
            call_kwargs["api_key"] = call_key
        if tools:
            call_kwargs["tools"] = tools
            call_kwargs.setdefault("tool_choice", "auto")
        return call_kwargs

    def _route(self, task_type: str | None) -> "LLMProvider":
        """The provider that may send a call of ``task_type`` (model-routing phase 0, the vendor guard).

        A task type routed to another vendor's model is sent by that vendor's own sibling (``for_model``), with its
        own key and none of the default's base or reasoning effort: this provider's key was pinned at start and went
        to whichever vendor a routing slot named. A routed model the catalogue does not list, a key that is not set,
        or a customer paying with their own key is refused here -- before any reservation. Siblings are not cached: a
        key rotated or deleted in the settings takes effect on the next call.
        """
        model = self._select_model(task_type)
        default = self.config.default_model
        if not task_type or not model or model == default:
            return self
        mine, theirs = _provider_prefix(model), _provider_prefix(default)
        if mine and mine == theirs:
            resolver = self._model_key_env
            if callable(resolver) and not str(resolver(model) or "").strip():
                raise RouteRefused(f"model {model!r} is not in the model catalogue", code="model_not_listed", model=model)
            return self
        customer_id, environment_id = current_credential_scope()
        try:
            return self.for_model(model, customer_id=customer_id, environment_id=environment_id)
        except RouteRefused:
            raise
        except Exception as exc:
            raise RouteRefused(f"the route to {model!r} could not be resolved ({type(exc).__name__})",
                               code="route_unresolved", model=model) from exc

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        task_type: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        before_dispatch: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        target = self._route(task_type)
        call_kwargs = target._prepare_call(messages, tools=tools, task_type=task_type, temperature=temperature,
                                           max_tokens=max_tokens, stream=False, extra=kwargs)
        return await target._send_chat(call_kwargs, before_dispatch=before_dispatch)

    @budgeted_call
    async def _send_chat(self, call_kwargs: dict[str, Any], *, before_dispatch: Any = None, **overrides: Any) -> dict[str, Any]:
        """Send one prepared request inside the budget reservation (the decorator's ``num_retries=0`` wins)."""
        call_kwargs = {**call_kwargs, **overrides}
        model = str(call_kwargs["model"])
        messages = list(call_kwargs.get("messages") or [])
        tools = call_kwargs.get("tools")
        from ampuly.computer_use.desktop_frames import prepare_outbound
        messages = prepare_outbound(self, model, messages)
        call_kwargs["messages"] = messages

        logger.debug(f"LLM call: model={model}, base={self._api_base or 'default'}, msgs={len(messages)}, tools={len(tools or [])}")

        try:
            if before_dispatch is not None:
                await _run_dispatch_check(before_dispatch)
            from ampuly.layer3_agent.employee_runtime import employee_dispatch_checkpoint
            await employee_dispatch_checkpoint("model")
            await before_model_dispatch(call_kwargs)
            await employee_dispatch_checkpoint("recheck")
            self._check_employee_dispatch_model(call_kwargs)
            from ampuly.computer_use.desktop_frame_outbound import before_frame_dispatch, consume_frame_dispatch
            before_frame_dispatch(self, call_kwargs)
            mark_dispatched()
            consume_frame_dispatch(self, call_kwargs)
            response = await self._acompletion(**call_kwargs)
        except Exception as e:
            logger.error("LLM call failed ({})", type(e).__name__)
            raise

        usage = getattr(response, "usage", None)
        cost = 0.0
        cost_known = False
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        observe_budget_usage(model=str(getattr(response, "model", "") or ""),
                             prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        usage_known = all(type(value) is int and value >= 0 for value in (prompt_tokens, completion_tokens))
        if usage_known:
            self._total_tokens_in += prompt_tokens
            self._total_tokens_out += completion_tokens
            try:
                quote = litellm.completion_cost(completion_response=response)
                if isinstance(quote, bool) or not isinstance(quote, (int, float)) or not math.isfinite(quote) or quote < 0:
                    raise ValueError("price not known")
                cost = float(quote)
                self._total_cost += cost
                cost_known = True
            except Exception:
                pass

        receipt = _CALL_RECEIPT.get()
        if receipt is not None:
            receipt.append({"sent": model, "served": str(getattr(response, "model", "") or "")})

        choice = response.choices[0]
        message = choice.message

        result: dict[str, Any] = {
            "content": message.content or "",
            "tool_calls": [],
            "finish_reason": choice.finish_reason,
            "model": model,
            "cost": cost,
            "cost_known": cost_known,
            "usage_known": usage_known,
            "usage": {
                "prompt_tokens": prompt_tokens if usage_known else 0,
                "completion_tokens": completion_tokens if usage_known else 0,
            },
        }

        if message.tool_calls:
            for tc in message.tool_calls:
                args, raw_args, parse_error = _parse_tool_arguments(tc.function.name, tc.function.arguments)
                tool_call = {
                    "id": tc.id,
                    "function": tc.function.name,
                    "arguments": args,
                }
                if raw_args is not None:
                    tool_call["arguments_raw"] = raw_args
                if parse_error:
                    tool_call["arguments_parse_error"] = parse_error
                result["tool_calls"].append(tool_call)

        return result

    def normalize_stream_event(
        self,
        chunk: Any,
        *,
        model: str,
    ) -> list[RuntimeLLMEvent]:
        events: list[RuntimeLLMEvent] = []
        usage = getattr(chunk, "usage", None)
        if usage:
            events.append(RuntimeLLMEvent(
                event_type="usage",
                model=model,
                payload={
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(usage, "completion_tokens", 0),
                    "context_window": self.get_context_window(model=model),
                },
            ))

        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return events

        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is not None:
            text = getattr(delta, "content", None)
            if text:
                events.append(RuntimeLLMEvent(
                    event_type="assistant_delta",
                    model=model,
                    payload={"text": text},
                ))
            thinking_text = (
                getattr(delta, "reasoning", None)
                or getattr(delta, "reasoning_content", None)
                or getattr(delta, "thinking", None)
            )
            if thinking_text:
                events.append(RuntimeLLMEvent(
                    event_type="thinking_delta",
                    model=model,
                    payload={"text": str(thinking_text)},
                ))
            tool_calls = getattr(delta, "tool_calls", None) or []
            for tool_call in tool_calls:
                function = getattr(tool_call, "function", None)
                events.append(RuntimeLLMEvent(
                    event_type="tool_call_delta",
                    model=model,
                    payload={
                        "index": getattr(tool_call, "index", 0),
                        "id": getattr(tool_call, "id", ""),
                        "name": getattr(function, "name", "") if function else "",
                        "arguments": getattr(function, "arguments", "") if function else "",
                    },
                ))

        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason:
            events.append(RuntimeLLMEvent(
                event_type="message_stop",
                model=model,
                payload={"finish_reason": finish_reason},
            ))
        return events

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        task_type: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        before_dispatch: Any = None,
        stream: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[RuntimeLLMEvent]:
        # Routed and prepared on the first iteration, before the reservation: a refusal raises here, nothing is yielded.
        if type(stream) is not bool:
            raise TypeError("stream must be a boolean")
        target = self._route(task_type)
        call_kwargs = target._prepare_call(messages, tools=tools, task_type=task_type, temperature=temperature,
                                           max_tokens=max_tokens, stream=stream, extra=kwargs)
        async with aclosing(target._send_stream(call_kwargs, before_dispatch=before_dispatch)) as events:
            async for event in events:
                yield event

    @budgeted_stream
    async def _send_stream(self, call_kwargs: dict[str, Any], *, before_dispatch: Any = None, **overrides: Any) -> AsyncIterator[RuntimeLLMEvent]:
        """Stream one prepared request inside the budget reservation (the decorator's ``num_retries=0`` wins)."""
        call_kwargs = {**call_kwargs, **overrides}
        model = str(call_kwargs["model"])
        messages = list(call_kwargs.get("messages") or [])
        tools = call_kwargs.get("tools")
        from ampuly.computer_use.desktop_frames import prepare_outbound
        messages = prepare_outbound(self, model, messages)
        call_kwargs["messages"] = messages

        logger.debug(
            f"LLM stream call: model={model}, base={self._api_base or 'default'}, msgs={len(messages)}, tools={len(tools or [])}"
        )

        last_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        yield RuntimeLLMEvent(event_type="message_start", model=model, payload={"model": model})

        stream = None
        # Whether anything of an answer arrived: a vendor's "no" counts as received only before that (C9).
        answered = False
        # The longest silence allowed before the next piece of the reply. The transport timeout does not cover a
        # stream that started and then stopped sending (B5, 2026-09-26: connected, no data, 10+ minutes); without
        # this the turn waits forever. None falls back to request_timeout; 0 or less means no limit.
        idle_setting = getattr(self.config, "stream_idle_timeout", None)
        if idle_setting is None:
            idle_setting = getattr(self.config, "request_timeout", 0) or 0
        try:
            idle = float(idle_setting)
        except (TypeError, ValueError):
            idle = 0.0
        # The error raised when that silence ran out: logged once, as the warning below, not again as a failure.
        idle_stop: Exception | None = None
        try:
            if before_dispatch is not None:
                await _run_dispatch_check(before_dispatch)
            from ampuly.layer3_agent.employee_runtime import employee_dispatch_checkpoint
            await employee_dispatch_checkpoint("model")
            await before_model_dispatch(call_kwargs)
            await employee_dispatch_checkpoint("recheck")
            self._check_employee_dispatch_model(call_kwargs)
            from ampuly.computer_use.desktop_frame_outbound import before_frame_dispatch, consume_frame_dispatch
            before_frame_dispatch(self, call_kwargs)
            mark_dispatched()
            consume_frame_dispatch(self, call_kwargs)
            stream = await self._acompletion(**call_kwargs)
            if hasattr(stream, "__aiter__"):
                chunks = stream.__aiter__()
                loop = asyncio.get_running_loop()
                while True:
                    waiting_since = loop.time()
                    pending = chunks.__anext__()
                    try:
                        chunk = await (asyncio.wait_for(pending, idle) if idle > 0 else pending)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError as exc:
                        if not isinstance(exc.__cause__, asyncio.CancelledError):
                            raise  # the transport's own timeout, not this silence window
                        waited = loop.time() - waiting_since
                        shown = f"{waited:.0f}" if waited >= 1 else f"{waited:.1f}"
                        if answered:
                            # Part of the answer came back: the vendor did work and may bill it -- outcome unknown
                            # (C9), never sent again (C10).
                            idle_stop = TimeoutError(f"模型回复到一半停住了：已经 {shown} 秒没有新内容。"
                                                     "这次调用的结果不确定、可能已经计费，不会自动重发")
                        else:
                            # Nothing came back at all: a connection failure -- after sending, unknown as well (C10).
                            idle_stop = ConnectionError(f"模型一直没有开始回复：等了 {shown} 秒没有收到任何内容，"
                                                        "连接可能已经断开。这次调用不会自动重发")
                        logger.warning("模型流式回复停住（{}）：model={}，已等 {:.1f} 秒没有新内容，按结果未知收场，不自动重发",
                                       "已收到部分内容" if answered else "一片内容都没收到", model, waited)
                        raise idle_stop from None
                    answered = True
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is not None:
                        observe_budget_usage(model=str(getattr(chunk, "model", "") or ""),
                            prompt_tokens=getattr(chunk_usage, "prompt_tokens", None),
                            completion_tokens=getattr(chunk_usage, "completion_tokens", None))
                    for event in self.normalize_stream_event(chunk, model=model):
                        if event.event_type == "usage":
                            total_prompt = int(event.payload.get("prompt_tokens", 0) or 0)
                            total_completion = int(event.payload.get("completion_tokens", 0) or 0)
                            delta_prompt = max(0, total_prompt - last_usage["prompt_tokens"])
                            delta_completion = max(0, total_completion - last_usage["completion_tokens"])
                            last_usage["prompt_tokens"] = total_prompt
                            last_usage["completion_tokens"] = total_completion
                            cost = 0.0
                            try:
                                prompt_cost, completion_cost = litellm.cost_per_token(
                                    model=model,
                                    prompt_tokens=delta_prompt,
                                    completion_tokens=delta_completion,
                                )
                                cost = float(prompt_cost or 0.0) + float(completion_cost or 0.0)
                            except Exception:
                                cost = 0.0
                            self._total_tokens_in += delta_prompt
                            self._total_tokens_out += delta_completion
                            self._total_cost += cost
                            event.payload = {
                                **dict(event.payload),
                                "prompt_tokens": delta_prompt,
                                "completion_tokens": delta_completion,
                                "prompt_tokens_total": total_prompt,
                                "completion_tokens_total": total_completion,
                                "estimated_cost_delta": cost,
                                "estimated_cost_total": self._total_cost,
                                "context_window": event.payload.get("context_window") or self.get_context_window(model=model),
                                "model": model,
                            }
                        yield event
            else:
                # Provider fallback: treat the response as a single non-streaming completion.
                answered = True
                choice = stream.choices[0]
                message = choice.message
                if getattr(message, "content", None):
                    yield RuntimeLLMEvent(
                        event_type="assistant_delta",
                        model=model,
                        payload={"text": message.content},
                    )
                # Each call keeps its own index: the runtime assembles tool calls by index, and giving every call 0
                # merged several calls of one reply into one malformed call (names and arguments concatenated).
                for index, tc in enumerate(getattr(message, "tool_calls", None) or []):
                    yield RuntimeLLMEvent(
                        event_type="tool_call_delta",
                        model=model,
                        payload={
                            "index": index,
                            "id": getattr(tc, "id", ""),
                            "name": getattr(getattr(tc, "function", None), "name", ""),
                            "arguments": getattr(getattr(tc, "function", None), "arguments", ""),
                        },
                    )
                usage = getattr(stream, "usage", None)
                if usage:
                    observe_budget_usage(model=str(getattr(stream, "model", "") or ""),
                        prompt_tokens=getattr(usage, "prompt_tokens", None),
                        completion_tokens=getattr(usage, "completion_tokens", None))
                    cost = 0.0
                    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                    try:
                        prompt_cost, completion_cost = litellm.cost_per_token(
                            model=model,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                        )
                        cost = float(prompt_cost or 0.0) + float(completion_cost or 0.0)
                    except Exception:
                        cost = 0.0
                    self._total_tokens_in += prompt_tokens
                    self._total_tokens_out += completion_tokens
                    self._total_cost += cost
                    yield RuntimeLLMEvent(
                        event_type="usage",
                        model=model,
                        payload={
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "prompt_tokens_total": prompt_tokens,
                            "completion_tokens_total": completion_tokens,
                            "estimated_cost_delta": cost,
                            "estimated_cost_total": self._total_cost,
                            "context_window": self.get_context_window(model=model),
                            "model": model,
                        },
                    )
                yield RuntimeLLMEvent(
                    event_type="message_stop",
                    model=model,
                    payload={"finish_reason": getattr(choice, "finish_reason", "stop")},
                )
        except Exception as e:
            if e is not idle_stop:
                logger.error(f"LLM stream failed: {e}")
            # A "no" the vendor sent before any of an answer carries its status (C9): the budget releases the
            # reservation for it, and the runtime reads the status, not the text. After a chunk the outcome is unknown.
            yield RuntimeLLMEvent(
                event_type="error",
                model=model,
                payload={"message": str(e), "status": None if answered else received_rejection(e),
                         "error_class": type(e).__name__},
            )
            raise

        finally:
            close = getattr(stream, "aclose", None)
            if callable(close):
                closed = close()
                if inspect.isawaitable(closed):
                    await closed

    async def simple_chat(
        self,
        prompt: str,
        system: str | None = None,
        task_type: str | None = None,
        meter: Any = None,
    ) -> str:
        """One question, one answer, and optionally a note of what it cost.

        ``chat`` already returns the model, the token counts and the cost;
        this used to discard all three, so every surface built on it -- the
        JSON-turn helper, the secretary, approval, company mode -- spent
        without appearing in any total.

        ``meter`` is supplied by the caller rather than reached for here,
        because this layer does not know which task a call belongs to and a
        row filed under an invented id reads as recorded while joining to
        nothing. A caller that passes none is unmetered, exactly as before,
        and visibly so.
        """
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        result = await self.chat(messages, task_type=task_type)
        if meter is not None:
            usage = dict(result.get("usage", {}) or {})
            try:
                await meter(
                    model=str(result.get("model", "") or ""),
                    tokens_in=int(usage.get("prompt_tokens", 0) or 0),
                    tokens_out=int(usage.get("completion_tokens", 0) or 0),
                    cost=float(result.get("cost", 0.0) or 0.0),
                )
            except Exception:
                # A failure to record must not lose the answer the owner is
                # waiting for. It is logged, not swallowed silently.
                logger.warning("simple_chat: the call could not be metered; this turn is not in the total")
        return result["content"]

    def get_tool_definitions(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert internal tool definitions to OpenAI function-calling format."""
        formatted = []
        for tool in tools:
            formatted.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                },
            })
        return formatted

    def _build_direct_attachment_parts(
        self,
        model: str,
        attachment_refs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        capabilities = self._attachment_capabilities(model)
        if not capabilities["enabled"]:
            return []

        parts: list[dict[str, Any]] = []
        for ref_dict in attachment_refs:
            try:
                ref = AttachmentRef.from_dict(ref_dict)
                data_url = self._attachment_data_url(ref)
            except Exception as exc:
                logger.warning(f"Skipping direct attachment payload: {exc}")
                continue

            if not data_url:
                continue

            if ref.mime_type.startswith("image/") and capabilities["image_mode"]:
                parts.append(self._build_attachment_part("image_url", ref, data_url))
            elif ref.mime_type == "application/pdf" and capabilities["pdf_mode"]:
                parts.append(self._build_attachment_part(str(capabilities["pdf_mode"]), ref, data_url))
            elif ref.mime_type.startswith("video/") and capabilities["video_mode"]:
                parts.append(self._build_attachment_part(str(capabilities["video_mode"]), ref, data_url))

        return parts

    def _build_attachment_part(
        self,
        mode: str,
        ref: AttachmentRef,
        data_url: str,
    ) -> dict[str, Any]:
        if mode == "image_url":
            return {
                "type": "image_url",
                "image_url": {"url": data_url},
            }
        if mode == "video_url":
            return {
                "type": "video_url",
                "video_url": {"url": data_url},
            }
        if mode == "file":
            return {
                "type": "file",
                "file": {
                    "file_data": data_url,
                    "filename": ref.filename,
                },
            }
        raise ValueError(f"Unsupported attachment transport mode: {mode}")

    def _attachment_capabilities(self, model: str) -> dict[str, Any]:
        provider = model.split("/", 1)[0].strip().lower()
        api_base = (self._api_base or "").lower()

        image_mode: str | None = None
        pdf_mode: str | None = None
        video_mode: str | None = None

        if "api.poe.com" in api_base:
            image_mode = "image_url"
            pdf_mode = "file"
            if _looks_like_multimodal_model(model):
                video_mode = "file"
        elif "openrouter.ai" in api_base:
            image_mode = "image_url"
            pdf_mode = "file"
            if _looks_like_video_capable_model(model):
                video_mode = "video_url"
        elif provider in {"openai", "azure", "anthropic"}:
            image_mode = "image_url"
            pdf_mode = "file"
        elif provider in {"google", "gemini", "vertex_ai", "vertex"}:
            image_mode = "image_url"
            pdf_mode = "file"
            if _looks_like_video_capable_model(model):
                video_mode = "video_url"
        elif _looks_like_multimodal_model(model):
            image_mode = "image_url"
            pdf_mode = "file" if _looks_like_document_capable_model(model) else None
            if _looks_like_video_capable_model(model):
                video_mode = "video_url"

        return {
            "enabled": bool(image_mode or pdf_mode or video_mode),
            "image_mode": image_mode,
            "pdf_mode": pdf_mode,
            "video_mode": video_mode,
        }

    def _attachment_data_url(self, ref: AttachmentRef) -> str | None:
        path = self._resolve_attachment_path(ref)
        if path is None:
            return None
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
        mime_type = ref.mime_type or _guess_mime_from_filename(ref.filename)
        return f"data:{mime_type};base64,{payload}"

    def _resolve_attachment_path(self, ref: AttachmentRef) -> Path | None:
        if not self.ampuly_home or not ref.disk_path:
            return None
        resolved = (self.ampuly_home / ref.disk_path).resolve()
        ampuly_home_resolved = self.ampuly_home.resolve()
        if not str(resolved).startswith(str(ampuly_home_resolved)):
            raise ValueError(f"Attachment path escapes Ampuly home: {ref.disk_path}")
        return resolved


def _guess_mime_from_filename(filename: str) -> str:
    suffix = attachment_suffix(filename)
    if suffix == ".pdf":
        return "application/pdf"
    if suffix == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if suffix == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if suffix == ".pptx":
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if suffix == ".mp4":
        return "video/mp4"
    if suffix in {".mpeg", ".mpg"}:
        return "video/mpeg"
    if suffix == ".mov":
        return "video/quicktime"
    if suffix == ".webm":
        return "video/webm"
    return "application/octet-stream"
