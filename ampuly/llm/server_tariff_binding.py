"""Existing server LLM config -> real provider CNY binding, no new side door.

Initialization and explicit reconfiguration load reviewed rows. Dispatch only
revalidates the captured source; it never auto-approves a changed row/account.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from threading import Lock
import weakref

from ampuly.core.model_tariff_config import validate_tariff_table
from ampuly.llm.budget import ModelBudgetStop
from ampuly.llm.priced_runtime import ServerTextTariff, bind_model_pricing, revoke_model_pricing

_BOUND = weakref.WeakKeyDictionary()
_LOCK = Lock()


def _stop():
    raise ModelBudgetStop("Server CNY tariff is missing, stale, expired or does not match this provider account") from None


def _now():
    return datetime.now(timezone.utc)


def _rows(provider):
    # model_copy and direct host assignment can bypass Pydantic validation;
    # validate again at this privileged boundary without reporting raw input.
    try:
        return validate_tariff_table(getattr(provider.config, "cny_text_tariffs", []))
    except Exception:
        _stop()


def _credential_source(provider):
    from ampuly.llm.provider import current_credential_scope
    customer_id, environment_id = current_credential_scope()
    resolver = getattr(provider, "_resolve_key_env", None)
    if customer_id and callable(resolver):
        try:
            variable = str(resolver(customer_id, environment_id) or "").strip()
        except Exception:
            _stop()
        if variable:
            return variable
    customer = getattr(provider, "_customer_key_env", "")
    if customer:
        return customer
    if getattr(provider.config, "api_key", ""):
        return ""  # An inline server config key is not an env-sourced account.
    return str(getattr(provider.config, "api_key_env", "") or "")


def _match(provider):
    model = provider.config.default_model
    endpoint = provider._api_base or ""
    source = _credential_source(provider)
    rows = [row for row in _rows(provider)
            if (row.model, row.endpoint, row.credential_env) == (model, endpoint, source)]
    if len(rows) != 1:
        return None
    return rows[0]


def _active(row):
    now = _now()
    return datetime.fromisoformat(row.valid_from) <= now < datetime.fromisoformat(row.valid_until)


def _row_digest(row):
    return hashlib.sha256(json.dumps(row.model_dump(mode="json"), ensure_ascii=False,
                       sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _tariff(row):
    names = ServerTextTariff.__dataclass_fields__
    values = {name: getattr(row, name) for name in names}
    values["served_models"] = tuple(values["served_models"])
    return ServerTextTariff(**values)


@dataclass(frozen=True)
class _SourceBinding:
    digest: str
    tariff: ServerTextTariff


def bind_server_text_tariff(provider) -> bool:
    """Only server composition calls this after configuration/route changes.

    A missing/expired row disables amount-authorized calls but does not break
    count-only legacy deployments. Invalid configured rows fail validation.
    No credential value or row is written, logged or sent to a client here.
    """
    revoke_model_pricing(provider)
    with _LOCK:
        _BOUND.pop(provider, None)
    row = _match(provider)
    if row is None or not _active(row):
        return False
    credential = provider._call_api_key()
    if type(credential) is not str or not credential.strip():
        return False  # Never authorize an account chosen later by LiteLLM.
    tariff = _tariff(row)
    bind_model_pricing(provider, tariff)
    with _LOCK:
        _BOUND[provider] = _SourceBinding(_row_digest(row), tariff)
    return True


def verify_server_text_tariff(provider, tariff: ServerTextTariff) -> None:
    """Final-send verification; a changed source requires explicit rebind."""
    with _LOCK:
        bound = _BOUND.get(provider)
    if bound is None:
        return  # Existing explicit server bind_model_pricing contract remains.
    row = _match(provider)
    if (row is None or not _active(row) or _row_digest(row) != bound.digest
            or tariff != bound.tariff):
        _stop()


def require_server_text_tariff(provider, *, model: str, max_tokens: int | None = None) -> dict:
    """Pure host preflight, not a dispatch permission or supplier invoice.

    Prepare/start may call this without creating a reservation. The final
    provider boundary still rechecks and reserves the complete bounded call.
    Explicit server bind_model_pricing contracts remain supported for tests
    and alternative reviewed hosts; wire metadata cannot bind that contract.
    """
    from ampuly.llm.priced_runtime import _binding, _credential_digest
    value = _binding(provider)
    tariff = value.tariff
    output_limit = provider.config.max_tokens if max_tokens is None else max_tokens
    if (model != tariff.model or provider.config.default_model != model
            or (provider._api_base or "default") != tariff.endpoint
            or _credential_digest(provider._call_api_key()) != value.credential_digest
            or type(output_limit) is not int or not 0 < output_limit <= tariff.output_token_limit):
        _stop()
    return dict(pricing_kind="rated_usage_not_invoice", policy_id=tariff.policy_id,
        revision=tariff.revision, model=tariff.model,
        input_token_limit=tariff.input_token_limit, output_token_limit=output_limit,
        maximum_micro_cny=tariff.charge(tariff.input_token_limit, output_limit))
