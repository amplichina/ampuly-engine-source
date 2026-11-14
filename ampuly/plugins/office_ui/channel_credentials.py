"""Whose account does this customer's message go out under.

One machine runs one set of channel credentials, so every message it sends
looks like it came from the same bot. That is fine until two customers share
the machine: acme's delivery notice then leaves under whatever account the
process happens to hold, and the only way to find out is that somebody
receives a message about a company they have never heard of.

A customer's ``channel`` credential reference names the variable holding the
**account identifier** their messages must go out under -- an app id, the
public half printed in a vendor console, never the secret. This asks the
running channel who it is and compares. Nothing here reads a secret, and
neither value is ever reported: the answer is "may send" or a refusal naming
the variable to look at.

Three refusals, and each is a different mistake:

- the variable is empty -- somebody wrote down the rule and never set it, so
  sending now would fall back to the house account;
- the channel is a different account -- the rule is set and this send breaks
  it, which is the failure the rule exists for;
- the channel cannot say who it is -- we were told to check and cannot, and
  "probably fine" is not a check.

A customer with no reference is unaffected, which is every installation that
has not configured this.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from ampuly.plugins.office_ui.credential_refs import (
    CredentialUnavailableError,
    require_env_var,
)


def channel_account_refusal(business: Any, channel: Any, effect: Any) -> str:
    """"" when this message may go out over this channel, else why not."""
    customer_id = str(getattr(effect, "customer_id", "") or "").strip()
    environment_id = str(getattr(effect, "environment_id", "") or "").strip()
    if not customer_id:
        # Nothing customer-scoped was asked for; this is the operator's own
        # traffic and has always used the machine's account.
        return ""
    try:
        env_var = require_env_var(
            business, purpose="channel", customer_id=customer_id,
            environment_id=environment_id, default_env="",
        )
    except CredentialUnavailableError as exc:
        return str(exc)
    if not env_var:
        return ""
    expected = str(os.environ.get(env_var, "") or "").strip()
    if not expected:
        # require_env_var already refuses an unset variable; reaching here
        # means it emptied between the two reads. Same answer either way.
        return f"{env_var} is not set, so the account for {customer_id} is unknown"
    identity = str(getattr(channel, "account_identity", lambda: "")() or "").strip()
    if not identity:
        return (
            f"messages for {customer_id} must go out under the account named in {env_var}, "
            f"and this channel cannot say which account it is running"
        )
    if identity != expected:
        return (
            f"messages for {customer_id} must go out under the account named in {env_var}; "
            f"this channel is running a different one"
        )
    return ""


def channel_credential_check(ampuly_home: Path | str, channel: Any) -> Callable[[Any], str]:
    """Bind the check to one channel, for the effect provider to ask per send.

    The business store is opened per call rather than held: a reference set in
    the console while the engine runs must take effect on the next send, not
    on the next restart.
    """
    home = Path(str(ampuly_home))

    def check(effect: Any) -> str:
        from ampuly.plugins.office_ui.business_store import BusinessStore

        business = BusinessStore(home)
        # The stop switch first, and bluntly: somebody who switched this
        # connector off does not want an approved send going out either, and
        # would reasonably assume one switch covered both paths.
        name = str(getattr(channel, "name", "") or "")
        switch = business.connector_switch(name)
        if not switch["enabled"]:
            return f"{name} is switched off ({switch['reason'] or 'no reason was recorded'})"
        return channel_account_refusal(business, channel, effect)

    return check
