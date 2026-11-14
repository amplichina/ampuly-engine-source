"""Which environment variable holds this customer's key -- never the key.

Two customers on one machine need different credentials for the same purpose,
and a delivery project must not quietly borrow the other's. What has been
missing is the mapping: today there is one global variable per provider, so
"whose key is this run using" has no answer.

The mapping stores a *name*, never a value. The key itself stays in the
approved environment mechanism, exactly as before -- this file just says which
variable to read for which customer, environment and purpose. A reference that
points at an unset variable resolves to "not available" with the name that is
missing, so an operator can fix it without anyone printing a secret.

Nothing here reads, logs or returns a credential value. The resolution
carries the variable name and whether it is set; the caller asks the
environment itself.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

# Purposes are ours, not a provider's: "the image model" outlives whichever
# vendor is behind it this quarter.
PURPOSES = ("model", "image", "channel", "storage")
# An environment variable name, and nothing that could smuggle a value.
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


class CredentialUnavailableError(RuntimeError):
    """This customer's key was configured somewhere that has nothing in it.

    Carried instead of a fallback, because the fallback is the bug: using the
    machine default when a customer's own variable is unset spends somebody
    else's account, or sends one customer's message from another's, and leaves
    no trace that it happened.
    """

    def __init__(self, resolution: "CredentialResolution") -> None:
        self.resolution = resolution
        who = resolution.customer_id or "this customer"
        if resolution.env_var:
            message = (
                f"the {resolution.purpose} credential for {who} is configured as "
                f"{resolution.env_var}, which is not set"
            )
        else:
            message = (
                f"the {resolution.purpose} credential for {who} could not be read, "
                "so whose account this would use is unknown"
            )
        super().__init__(message)


@dataclass(frozen=True)
class CredentialResolution:
    """Where a key would come from, and whether it is there."""

    purpose: str
    env_var: str = ""
    available: bool = False
    reason: str = ""
    customer_id: str = ""
    environment_id: str = ""

    def to_payload(self) -> dict[str, Any]:
        # Deliberately no value field: there is nowhere for one to leak from.
        return {
            "purpose": self.purpose,
            "env_var": self.env_var,
            "available": self.available,
            "reason": self.reason,
            "customer_id": self.customer_id,
            "environment_id": self.environment_id,
        }


def validate_env_var(name: str) -> str:
    clean = str(name or "").strip()
    if not ENV_NAME.match(clean):
        raise ValueError(
            "invalid environment variable name; "
            "store the name of the variable, never the key itself"
        )
    # Use the real child-process policy rather than a second reserved-name
    # list. Import locally: credential metadata users need not load runtime
    # configuration at module import (a cold runtime import has host setup).
    from ampuly.layer4_tools.execution_context import subprocess_env_inherits

    if subprocess_env_inherits(clean):
        raise ValueError(
            "credential environment variable name is reserved for subprocess startup"
        )
    return clean


def resolve_credential(
    business: Any,
    *,
    purpose: str,
    customer_id: str = "",
    environment_id: str = "",
) -> CredentialResolution:
    """The most specific reference that applies, and whether it is set.

    Specific beats general -- (customer, environment) before (customer) before
    the machine-wide default -- because the narrower one is the deliberate
    exception somebody wrote down.
    """
    clean_purpose = str(purpose or "").strip()
    if clean_purpose not in PURPOSES:
        return CredentialResolution(purpose=clean_purpose, reason="unknown_purpose")
    try:
        reference = business.credential_reference(
            purpose=clean_purpose, customer_id=customer_id, environment_id=environment_id,
        )
    except Exception:
        # Fail closed: a reference we cannot read is not a reference we may
        # guess at.
        return CredentialResolution(purpose=clean_purpose, reason="reference_unreadable",
                                    customer_id=str(customer_id or ""), environment_id=str(environment_id or ""))
    if not reference:
        return CredentialResolution(
            purpose=clean_purpose, reason="no_reference",
            customer_id=str(customer_id or ""), environment_id=str(environment_id or ""),
        )
    try:
        stored_name = reference.get("env_var", "")
        env_var = validate_env_var(stored_name)
        if type(stored_name) is not str or env_var != stored_name:
            raise ValueError("stored credential reference name is not canonical")
    except ValueError:
        # Legacy metadata is not migrated or silently replaced by a default.
        # Refuse before any environment lookup, without returning its content.
        return CredentialResolution(
            purpose=clean_purpose, reason="reference_invalid",
            customer_id=str(customer_id or ""),
            environment_id=str(environment_id or ""),
        )
    present = bool(str(os.environ.get(env_var, "") or "").strip())
    return CredentialResolution(
        purpose=clean_purpose,
        env_var=env_var,
        available=present,
        reason="" if present else "env_var_not_set",
        customer_id=str(reference.get("customer_id", "") or ""),
        environment_id=str(reference.get("environment_id", "") or ""),
    )


def require_env_var(
    business: Any,
    *,
    purpose: str,
    customer_id: str = "",
    environment_id: str = "",
    default_env: str = "",
) -> str:
    """Which variable this call must read -- or a refusal naming the empty one.

    Three call paths need the same decision (the image key, the model key, the
    channel a customer's message goes out on) and the rule must not be written
    three times, because the third copy is where it stops matching.

    The rule: a run that names no customer keeps the machine default, which is
    what every existing installation already does. A customer with no
    reference keeps it too -- configuring nothing changes nothing. A customer
    who *has* a reference gets theirs, and if theirs is empty the call stops
    here. That last case is the whole point: silently falling back would spend
    or send on an account nobody chose.

    Failing to read the mapping is also a stop. We asked whose key this is and
    could not find out; guessing "probably the house one" is the same mistake
    with an excuse.
    """
    if str(purpose or "").strip() not in PURPOSES:
        raise ValueError(f"unknown credential purpose {purpose!r}")
    if not str(customer_id or "").strip() and not str(environment_id or "").strip():
        return default_env
    resolution = resolve_credential(
        business, purpose=purpose, customer_id=customer_id, environment_id=environment_id,
    )
    if resolution.reason in {"reference_unreadable", "reference_invalid"}:
        raise CredentialUnavailableError(resolution)
    if not resolution.env_var:
        return default_env
    if not resolution.available:
        raise CredentialUnavailableError(resolution)
    return resolution.env_var


def preflight(
    business: Any,
    purposes: tuple[str, ...],
    *,
    customer_id: str = "",
    environment_id: str = "",
    defaults: dict[str, str] | None = None,
) -> list[CredentialResolution]:
    """Which of these purposes would stop the run, asked before anything starts.

    A run that dies on its third step because a variable was never set costs
    the two steps before it and a person's afternoon. This answers the same
    question without spending anything, so the console can say what is missing
    while somebody is still looking at it.
    """
    blocked: list[CredentialResolution] = []
    for purpose in purposes:
        try:
            require_env_var(
                business, purpose=purpose, customer_id=customer_id,
                environment_id=environment_id, default_env=(defaults or {}).get(purpose, ""),
            )
        except CredentialUnavailableError as exc:
            blocked.append(exc.resolution)
        except ValueError:
            continue
    return blocked
