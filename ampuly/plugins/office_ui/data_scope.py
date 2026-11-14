"""Who may see which file of a project, decided from the path alone -- data phase 2, D4.

One rule for the whole 「资料」 entry: the websocket readers (D6) and the download route (D7) ask the same question
here, so a listing never shows what a download would refuse, and neither can drift from the other.

It decides before any disk access, from the path's own shape (``order_layout.classify``) and, for an order folder,
that order's customer as the request already looked it up. It never reads the disk, never writes and never raises on
an ordinary refusal: it answers.

In this phase only the owner reaches the entry at all (the role table refuses everyone else first). The employee and
bound-account rules are written and tested here so that opening the entry to employees later is a role change, not
new scope code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ampuly.core.order_layout import (
    AUDIT,
    DELIVERY_EXPORTS,
    DELIVERY_HISTORY,
    DOWNLOADS,
    INPUTS,
    LEGACY_DOWNLOADS,
    MATERIALS_COMMON,
    MATERIALS_CUSTOMERS,
    ORDERS,
    OUTPUT,
    PORTFOLIO_EXPORTS,
    PROJECT_MATERIALS,
    STEP_TABLE,
    STEPS,
    classify,
    customer_segment,
)

# Names the layout gives a meaning to. A component that casefolds to one of these without being spelled exactly so is
# refused: on a disk that ignores case it would reach the same folder under a spelling no listing ever showed.
_KEYWORDS = frozenset({
    ORDERS, DELIVERY_HISTORY, DELIVERY_EXPORTS, PORTFOLIO_EXPORTS, PROJECT_MATERIALS,
    MATERIALS_COMMON, MATERIALS_CUSTOMERS,
    OUTPUT, STEPS, INPUTS, AUDIT, LEGACY_DOWNLOADS, STEP_TABLE,
})
_HIDDEN_PREFIXES = (".", "__pycache__", "node_modules")


@dataclass(frozen=True)
class Viewer:
    """Who is asking: the owner, an employee account, or one bound to customers."""

    owner: bool = False
    customers: tuple[str, ...] = ()

    @property
    def bound(self) -> bool:
        return bool(self.customers) and not self.owner

    def has_customer(self, customer_id: str) -> bool:
        clean = str(customer_id or "").strip()
        return bool(clean) and clean in self.customers


@dataclass(frozen=True)
class Decision:
    """Whether this viewer may see this path, and what the path is."""

    allowed: bool
    reason: str = ""
    area: str = ""
    order_id: str = ""
    customer_id: str = ""


def project_gate(viewer: Viewer, project_customer: str | None) -> str | None:
    """Why this viewer may not enter the project at all, or ``None``.

    The owner and an unbound employee may enter any project. A bound account may enter only a project whose own
    customer is one of theirs; a project bound to nobody, or a binding the caller could not read (``None``), is
    refused -- with several customers' orders in one project there is no per-record answer yet.
    """
    if viewer.owner or not viewer.bound:
        return None
    if project_customer is None:
        return "project_scope_unknown"
    if not viewer.has_customer(project_customer):
        return "project_not_yours"
    return None


def path_parts(relative_path: str) -> tuple[str, ...]:
    """The path's components, or ``()`` when it is not a plain relative path below the project."""
    # Not stripped: a name ending in a space or a dot is a different name on Windows, and is refused below.
    candidate = str(relative_path or "").replace("\\", "/")
    if not candidate:
        return ()
    parts = tuple(part for part in candidate.split("/") if part != "")
    if not parts or ":" in candidate or "\x00" in candidate:
        return ()
    for part in parts:
        # Either end: a trailing space or dot is dropped by Windows, and a leading space was dropped by the
        # reader beside this rule (``artifact_path_parts``) until 2026-09-23 -- so one space made the two
        # disagree about which file was asked for, and the scope refused a path the reader then opened.
        if part in {".", ".."} or part.strip() != part or part.rstrip(".") != part:
            return ()
    return parts


def hidden(parts: Iterable[str]) -> bool:
    return any(str(part).startswith(_HIDDEN_PREFIXES) for part in parts)


def _spelled_exactly(parts: Iterable[str]) -> bool:
    """Every component that means something to the layout is spelled the way the layout writes it."""
    for part in parts:
        folded = str(part).casefold()
        if folded in _KEYWORDS and str(part) != folded:
            return False
    return True


def decide(viewer: Viewer, relative_path: str, order_customer: Callable[[str], str | None]) -> Decision:
    """Whether ``viewer`` may see ``relative_path`` under the project, and what that path is.

    ``order_customer(order_id)`` gives the customer of a business order of **this** project, or ``None`` when the
    project has no such order (a folder left by an older run, keyed by a task id, answers ``None``).
    """
    parts = path_parts(relative_path)
    if not parts:
        return Decision(False, "invalid_path")
    if hidden(parts):
        return Decision(False, "hidden")
    if not _spelled_exactly(parts):
        return Decision(False, "invalid_path")
    facts = classify("/".join(parts))
    area = str(facts.get("area", "") or "")
    order_id = str(facts.get("order_id", "") or "")

    if area == PROJECT_MATERIALS:
        return _materials(viewer, parts, facts)
    if area == PORTFOLIO_EXPORTS:
        # A portfolio export carries several orders' work: the owner's alone.
        return Decision(viewer.owner, "" if viewer.owner else "owner_only", area=area)
    if order_id:
        return _order_area(viewer, parts, area, order_id, order_customer)
    # Anything else that is not hidden: a plain session's own files, including its ``materials/``. The project gate
    # has already decided whether this viewer may be in this project at all.
    return Decision(True, area=area)


def _materials(viewer: Viewer, parts: tuple[str, ...], facts: dict[str, Any]) -> Decision:
    customer = str(facts.get("customer", "") or "")
    if customer == MATERIALS_COMMON:
        return Decision(True, area=PROJECT_MATERIALS, customer_id=MATERIALS_COMMON)
    if len(parts) >= 2 and parts[1] == MATERIALS_CUSTOMERS:
        if viewer.owner or not viewer.bound:
            return Decision(True, area=PROJECT_MATERIALS, customer_id=customer)
        if customer and any(customer_segment(own) == customer for own in viewer.customers):
            return Decision(True, area=PROJECT_MATERIALS, customer_id=customer)
        return Decision(False, "not_your_customer", area=PROJECT_MATERIALS, customer_id=customer)
    # Directly under the material area, or a folder of its own: everyone but a bound account, whose share of the
    # material is the customers' folders and the common one.
    if viewer.bound:
        return Decision(False, "not_your_customer", area=PROJECT_MATERIALS)
    return Decision(True, area=PROJECT_MATERIALS)


def _order_area(viewer: Viewer, parts: tuple[str, ...], area: str, order_id: str,
                order_customer: Callable[[str], str | None]) -> Decision:
    if area == STEP_TABLE or (len(parts) >= 3 and parts[2] == STEP_TABLE):
        # The step table is read by the order view, never served as a file.
        return Decision(False, "not_served", area=area, order_id=order_id)
    known = order_customer(order_id)
    if known is None:
        # No business order of this project: a folder from an older run, keyed by a task id. The owner may look.
        return Decision(viewer.owner, "" if viewer.owner else "owner_only", area=area, order_id=order_id)
    if area in {AUDIT, DOWNLOADS}:
        # A browser audit export records what was done on a customer's own systems: the owner's alone.
        return Decision(viewer.owner, "" if viewer.owner else "owner_only", area=area, order_id=order_id)
    if viewer.owner or not viewer.bound:
        return Decision(True, area=area, order_id=order_id, customer_id=known)
    if viewer.has_customer(known):
        return Decision(True, area=area, order_id=order_id, customer_id=known)
    return Decision(False, "not_your_customer", area=area, order_id=order_id, customer_id=known)
