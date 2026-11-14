"""Where every project's orders stand, in one read, for the all-projects level of 项目中心.

The console holds one project's orders at a time, so the list of projects could say nothing about any of them but
the open one.  This projection hands back, per project, the few facts of each order that the page's own queue
function reads -- status, acceptance status, flow -- and nothing else: no customer, no brand, no fields.  The page
counts them with the same function 业务工单 sorts by, so a number on the overview cannot disagree with the list it
opens, and there is no second copy of the rule on the server.

Read only.  Plugin-layer module: no engine-kernel changes.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Iterable

from ampuly.plugins.office_ui.business_store import ORDER_LIST_HARD_CAP
from ampuly.plugins.office_ui.order_attention import flows_of, standing_of

# What one order contributes.  Deliberately not the order: the overview is about how much stands where.
_FACTS = ("order_id", "status", "workflow_id")
# S0-A (2026-09-27): the judgement of one project's rows -- ``(project_id, rows) -> (verdicts by order id, partial)``
# (``order_attention.judge_project`` behind the console's own readers).  With one, each fact carries ``attention``
# and each project its ``standing`` and ``flows``, counted here from the same verdicts: one rule, one place.
Judge = Callable[[str, list[dict[str, Any]]], Awaitable[tuple[dict[str, dict[str, Any]], bool]]]
PER_PROJECT_LIMIT = 200
# The order store hands back at most ORDER_LIST_HARD_CAP rows whatever it is asked for (the store's own constant, not
# a copy of its value).  "There were more" is seen by asking for one row past the limit, so the limit has to stay
# below it -- at the ceiling that extra row could never arrive.


def order_facts(order: dict[str, Any], attention: dict[str, Any] | None = None) -> dict[str, Any]:
    acceptance = order.get("acceptance") if isinstance(order.get("acceptance"), dict) else {}
    facts: dict[str, Any] = {key: str(order.get(key, "") or "") for key in _FACTS}
    facts["acceptance"] = {"status": str(acceptance.get("status", "") or "")}
    if attention is not None:
        facts["attention"] = dict(attention)
    return facts


def project_order_rows(api: Any, project_ids: Iterable[str], *, limit: int = PER_PROJECT_LIMIT) -> dict[str, tuple[list[dict[str, Any]], bool]]:
    """The newest ``limit`` order rows of each given project, and whether it holds more, by project id."""
    limit = max(1, min(int(limit), ORDER_LIST_HARD_CAP - 1))
    found: dict[str, tuple[list[dict[str, Any]], bool]] = {}
    for raw in project_ids:
        project_id = str(raw or "").strip()
        if not project_id or project_id in found:
            continue
        # One row past the limit: the store stops at whatever it is asked for, so asking for exactly ``limit`` could
        # never show that anything was left out.
        orders = list(api.orders(project_id=project_id, limit=limit + 1) or [])
        found[project_id] = ([order for order in orders[:limit] if isinstance(order, dict)], len(orders) > limit)
    return found


def project_order_overview(api: Any, project_ids: Iterable[str], *, limit: int = PER_PROJECT_LIMIT) -> dict[str, Any]:
    """``{"projects": {project_id: {"orders": [...facts], "truncated": bool}}}`` for the given projects only.

    The caller decides which projects are visible; a project that is not handed in is not read.  ``truncated`` is true
    when the project has more orders than the newest ``limit`` handed back: the counts then cover only those.
    """
    projects = {
        project_id: {"orders": [order_facts(order) for order in rows], "truncated": truncated}
        for project_id, (rows, truncated) in project_order_rows(api, project_ids, limit=limit).items()
    }
    return {"projects": projects}


async def project_order_overview_judged(
    api: Any, project_ids: Iterable[str], *, judge: Judge, limit: int = PER_PROJECT_LIMIT,
) -> dict[str, Any]:
    """The overview above with the S0-A judgement: each fact carries ``attention``, each project ``standing``
    (the four numbers, ``truncated``, ``partial``) and ``flows`` (the four numbers per flow), and ``partial_projects``
    names the projects whose pauses or budgets could not be read -- their waiting and abnormal numbers are unknown,
    never zero said as if known."""
    projects: dict[str, Any] = {}
    partial_projects: list[str] = []
    for project_id, (rows, truncated) in project_order_rows(api, project_ids, limit=limit).items():
        verdicts, partial = await judge(project_id, rows)
        if partial:
            partial_projects.append(project_id)
        projects[project_id] = {
            "orders": [order_facts(order, verdicts.get(str(order.get("order_id", "") or ""))) for order in rows],
            "truncated": truncated,
            "standing": standing_of(rows, verdicts, truncated=truncated, partial=partial),
            "flows": flows_of(rows, verdicts),
        }
    return {"projects": projects, "partial_projects": partial_projects}
