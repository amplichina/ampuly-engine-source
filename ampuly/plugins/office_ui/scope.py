"""One place that decides whose data a request may touch.

The checks existed, but each entry had its own: the order list narrowed one
way, deliverables another, projects a third. Three implementations of one
rule is three chances to disagree, and the one that disagrees is the one an
employee finds.

So the intersection lives here, and the entries call it. It only ever
narrows: an account bound to a customer cannot widen its own reach by
asking, an unbound project is "not yet assigned" rather than everyone's, and
a request that cannot be attributed is refused rather than guessed at.

Environment works the same way but is not yet a permission: a project may
declare one, and a request naming a different environment is refused. What
this file will not do is invent an environment for a request that named
none -- an unstated environment is unknown, not "the default one".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScopeDecision:
    """What a request may see, and why it was cut down."""

    allowed: bool
    customer_id: str = ""
    environment_id: str = ""
    reason: str = ""

    @property
    def denied(self) -> bool:
        return not self.allowed


def resolve_customer(
    *,
    account_customers: tuple[str, ...],
    project_customer: str = "",
    requested_customer: str = "",
) -> ScopeDecision:
    """Intersect who is asking, where they are asking, and what they asked for.

    ``account_customers`` empty means the account is unrestricted (the owner).
    ``project_customer`` empty means the project is not yet assigned, which is
    a refusal for a bound account rather than an invitation.
    """
    bound = tuple(str(c).strip() for c in account_customers if str(c).strip())
    project = str(project_customer or "").strip()
    asked = str(requested_customer or "").strip()

    if asked and project and asked != project:
        return ScopeDecision(False, reason="customer_conflicts_with_project")

    if not bound:
        # Unrestricted: the project's own binding still decides attribution.
        return ScopeDecision(True, customer_id=asked or project)

    if project:
        if project not in bound:
            return ScopeDecision(False, reason="project_belongs_to_another_customer")
        if asked and asked != project:
            return ScopeDecision(False, reason="customer_scope_denied")
        return ScopeDecision(True, customer_id=project)

    # A bound account in an unassigned project: fail closed. Somebody has to
    # say whose space it is before its contents can be read.
    if asked:
        return ScopeDecision(False, reason="project_customer_unbound")
    return ScopeDecision(False, reason="project_customer_unbound")


def resolve_environment(*, project_environment: str = "", requested_environment: str = "") -> ScopeDecision:
    """An explicit environment must match the project's; silence stays silent."""
    project = str(project_environment or "").strip()
    asked = str(requested_environment or "").strip()
    if asked and project and asked != project:
        return ScopeDecision(False, reason="environment_conflicts_with_project")
    return ScopeDecision(True, environment_id=asked or project)


def resolve(
    *,
    account_customers: tuple[str, ...] = (),
    project_customer: str = "",
    project_environment: str = "",
    requested_customer: str = "",
    requested_environment: str = "",
) -> ScopeDecision:
    """The whole decision: customer first, then environment."""
    customer = resolve_customer(
        account_customers=account_customers,
        project_customer=project_customer,
        requested_customer=requested_customer,
    )
    if customer.denied:
        return customer
    environment = resolve_environment(
        project_environment=project_environment,
        requested_environment=requested_environment,
    )
    if environment.denied:
        return environment
    return ScopeDecision(True, customer_id=customer.customer_id, environment_id=environment.environment_id)
