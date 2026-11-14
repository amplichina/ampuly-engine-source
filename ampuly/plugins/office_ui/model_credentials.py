"""Whose account pays for this project's model calls.

The engine authenticates with whatever key the process started with. On a
machine serving one customer that is correct and invisible. On a machine
serving several it is a billing question nobody can answer afterwards: the
usage lands on one account and the work belongs to another, and no record
says which run did it.

A project belongs to a customer, and a customer may have a ``model``
credential reference: the name of the variable holding their key. Applying it
when a project's engine is activated is enough, because an engine is bound to
one project at a time -- the same reason this is safe is the reason it must
be re-applied on every switch.

Two outcomes are useful and one is not. Applying their variable is the point.
Leaving the machine default alone when they have no reference is what every
existing installation depends on. Falling back to the machine default when
they *do* have a reference and it is empty is the one this refuses: that
spends an account nobody chose, and the only evidence would be an invoice.
"""

from __future__ import annotations

from typing import Any

from ampuly.plugins.office_ui.credential_refs import (
    CredentialResolution,
    CredentialUnavailableError,
    require_env_var,
)


def apply_model_credential(
    engine: Any,
    business: Any,
    *,
    customer_id: str,
    environment_id: str = "",
) -> str:
    """Point this engine's model calls at the customer's key. Returns the name.

    Returns "" when no customer or no reference is configured. A configured
    reference with an empty variable, or a provider unable to bind that
    reference, raises ``CredentialUnavailableError``. The caller must not
    continue the customer's call on a different account.
    """
    if not str(customer_id or "").strip():
        return ""
    env_var = require_env_var(
        business, purpose="model", customer_id=customer_id,
        environment_id=environment_id, default_env="",
    )
    if not env_var:
        return ""
    llm = getattr(engine, "llm", None)
    use_key_env = getattr(llm, "use_key_env", None)
    if not callable(use_key_env):
        raise CredentialUnavailableError(CredentialResolution(
            purpose="model", customer_id=customer_id, environment_id=environment_id,
            reason="provider_credential_binding_unsupported",
        ))
    if not use_key_env(env_var):
        # The reference said it was set a moment ago. Whatever emptied it, the
        # answer is the same one require_env_var would have given.
        raise CredentialUnavailableError(
            _unavailable(env_var, customer_id, environment_id)
        )
    return env_var


def bind_model_credential_resolver(engine: Any, business: Any) -> bool:
    """Let each model call ask which variable its own customer uses.

    Applying one customer's key to the engine is right while an engine
    serves one project at a time. The moment two customers are in flight in
    one process it is wrong, and wrong silently: the second one's calls
    spend the first one's account and only the invoice knows.

    Only the variable *name* crosses back. A customer with no reference
    resolves to "", which the provider reads as "use what the process
    started with" -- the behaviour every installation has today.
    """
    llm = getattr(engine, "llm", None)
    bind = getattr(llm, "bind_credential_resolver", None)
    if not callable(bind):
        return False

    def _resolve(customer_id: str, environment_id: str = "") -> str:
        if not str(customer_id or "").strip():
            return ""
        return require_env_var(
            business, purpose="model", customer_id=customer_id,
            environment_id=environment_id, default_env="",
        )

    bind(_resolve)
    return True


def apply_project_model_credential(engine: Any, business: Any, project_id: str) -> str:
    """The same thing, for a project whose customer the business layer knows."""
    try:
        customer_id = str(business.project_customer(str(project_id or "")) or "").strip()
    except Exception:
        # An unreadable ownership mapping is not an explicitly unbound project.
        raise CredentialUnavailableError(CredentialResolution(
            purpose="model", reason="project_customer_unavailable",
        )) from None
    if not customer_id:
        return ""
    return apply_model_credential(engine, business, customer_id=customer_id)


def _unavailable(env_var: str, customer_id: str, environment_id: str):
    from ampuly.plugins.office_ui.credential_refs import CredentialResolution

    return CredentialResolution(
        purpose="model", env_var=env_var, available=False, reason="env_var_not_set",
        customer_id=customer_id, environment_id=environment_id,
    )
