"""安安起草工单 -- the one way master control starts work (policy, M1, option B).

Work starts from a work order (业务工单); 安安 on 总控 is for questions and drafting. So master control never starts a
run, never opens a session and never spends: ``master_create_order`` drafts ONE order on a flow this project takes
orders on, through the console's own entry (``ConsoleAPI.create_order``: flow ownership, adoption and version, the
project's customer binding), in 「待接单」 (``pending_review``). The owner releases it in 业务工单 like any other order.

What the loop's tool, the console handler and the single-call path share lives here, so there is one answer to
"which flows can this project take an order on" and one set of checks a draft passes before an order exists:

* :func:`order_flows` -- the flows this project takes orders on now, each with its start fields and the sites its
  browser steps may open; and the ones it cannot, with why in the owner's words. Another project's flow is not named.
* :func:`check_draft` -- the same rules for one draft, plus its start fields: an unknown field name or a missing
  required one refuses the draft (nothing is dropped silently, nothing is guessed).

Nothing here writes. The words are the owner's: plain Chinese, no internal ids.
"""

from __future__ import annotations

import json
from typing import Any, Callable

# The order's ``source`` and the word the owner reads wherever sources are named (project_connections, 业务工单).
ORDER_SOURCE = "master"
SOURCE_WORD = "安安起草"

# What master control says when a flow belongs to another project (master_loop.OTHER_PROJECTS_FLOW says the same).
_OTHER_PROJECTS_ZH = "这条业务流属于别的项目，这里不能用它开单"
# One start-field value, and all of them together, as text. A list a batch flow takes is bounded by the second.
MAX_VALUE_CHARS = 20_000
MAX_FIELDS_CHARS = 64 * 1024

AdoptionReader = Callable[[str, str], "dict[str, Any] | None"]


class DraftRefused(Exception):
    """A draft this project cannot place: ``code`` for the model, ``zh`` / ``en`` for whoever reads it."""

    def __init__(self, code: str, zh: str, en: str) -> None:
        super().__init__(en)
        self.code = code
        self.zh = zh
        self.en = en


def adoption_reader(handler: Any) -> AdoptionReader | None:
    """How this console reads a project's adoption of a flow; None for a console without a business layer."""
    console = getattr(handler, "_console_api", None)
    if not callable(console):
        return None

    def read(project_id: str, workflow_id: str) -> dict[str, Any] | None:
        return console().business.workflow_adoption(project_id, workflow_id)

    return read


def find_package(packages: list[Any], workflow_id: str) -> Any | None:
    wanted = str(workflow_id or "").strip()
    return next((p for p in packages if str(getattr(p, "id", "") or "") == wanted), None) if wanted else None


def _usable(package: Any, project_id: str) -> bool:
    """A package without a manifest (a test double) has no owner and is taken as global, as master_loop does."""
    from ampuly.plugins.office_ui.workflow_packages import usable_by_project

    return not isinstance(getattr(package, "manifest", None), dict) or usable_by_project(package, project_id)


def _version(package: Any) -> str:
    manifest = getattr(package, "manifest", None)
    return str((manifest or {}).get("version", "") or "").strip() if isinstance(manifest, dict) else ""


def not_taking_orders(package: Any, project_id: str, adoption_of: AdoptionReader | None) -> DraftRefused | None:
    """Why this project cannot take an order on ``package`` right now, or None when it can.

    The console's own rules, in its order: another project's flow; a flow that is not ready (a draft, retired, or with
    problems -- the launch would refuse it); an adoption that is paused, withdrawn or on another version
    (``ConsoleAPI.create_order`` refuses those too). An adoption that cannot be read is not taken as "none".
    """
    if not _usable(package, project_id):
        return DraftRefused("other_projects_flow", _OTHER_PROJECTS_ZH, "that flow belongs to another project")
    if not bool(getattr(package, "invokable", False)):
        status = str(getattr(package, "status", "") or "")
        if status == "draft":
            return DraftRefused("flow_not_ready", "这条业务流还是草稿，还不能接单", f"flow {package.id!r} is a draft; it takes no orders")
        if status == "retired":
            return DraftRefused("flow_not_ready", "这条业务流已经停用，不再接单", f"flow {package.id!r} is retired")
        return DraftRefused(
            "flow_not_ready", "这条业务流有问题还没解决（在搭建台能看到），暂时不能接单",
            f"flow {package.id!r} is not invokable: " + "; ".join(str(p) for p in list(getattr(package, "problems", []) or [])[:3]),
        )
    if adoption_of is None:
        return None
    try:
        adoption = adoption_of(project_id, str(package.id))
    except Exception as exc:  # noqa: BLE001 - an unreadable adoption is not "never adopted"
        return DraftRefused(
            "adoption_unreadable", "读不到这个项目对这条业务流的采用记录，暂时不能起草",
            f"the project's adoption of {package.id!r} could not be read: {type(exc).__name__}",
        )
    if not adoption:
        # Never adopted: the old global path, as ConsoleAPI.create_order keeps it.
        return None
    status = str(adoption.get("status", "") or "")
    if status == "paused":
        return DraftRefused("adoption_paused", "这个项目在这条业务流上暂停接单了，要先在项目里恢复", f"the project paused new orders on {package.id!r}")
    if status != "active":
        return DraftRefused("adoption_withdrawn", "这个项目已经撤下这条业务流，不在上面接单", f"the project's adoption of {package.id!r} is {status or 'unknown'}")
    adopted, available = str(adoption.get("workflow_version", "") or ""), _version(package)
    if adopted != available:
        return DraftRefused(
            "adoption_behind",
            f"这个项目采用的是这条业务流的 {adopted or '旧'} 版，现在能跑的是 {available or '另一'} 版，要先在项目里升级采用",
            f"the project adopted {package.id!r} {adopted}, but {available or 'no version'} is what can run now",
        )
    return None


def start_fields(package: Any) -> list[dict[str, Any]]:
    """The flow's start fields as the order form shows them: id, label, required (and multiline when it is)."""
    rows: list[dict[str, Any]] = []
    for spec in list(getattr(package, "intake_fields", []) or []):
        field_id = str(spec.get("id", "") or "").strip()
        if not field_id:
            continue
        rows.append({
            "id": field_id,
            "label": str(spec.get("label", "") or field_id),
            "required": bool(spec.get("required")),
            **({"multiline": True} if spec.get("multiline") else {}),
        })
    return rows


def flow_sites(package: Any) -> list[str]:
    """The sites this flow's browser steps may open: an order works only there (X05's domains left with M1)."""
    from ampuly.plugins.office_ui.browser_connector import connector_summary

    sites: set[str] = set()
    for node in list(getattr(package, "nodes", []) or []):
        summary = connector_summary(node) if isinstance(node, dict) else None
        sites.update((summary or {}).get("allowed_domains", []) or [])
    return sorted(sites)


def order_flows(packages: list[Any], project_id: str, adoption_of: AdoptionReader | None) -> dict[str, Any]:
    """What ``master_list_order_flows`` answers: flows this project takes orders on, and the ones it does not, with why.

    Another project's flow is left out of both lists: one project is one customer, and that flow is theirs.
    """
    taking: list[dict[str, Any]] = []
    not_taking: list[dict[str, Any]] = []
    for package in packages:
        if not _usable(package, project_id):
            continue
        refused = not_taking_orders(package, project_id, adoption_of)
        name = str(getattr(package, "name", "") or getattr(package, "id", ""))
        if refused is not None:
            not_taking.append({"workflow_id": str(package.id), "name": name, "why": refused.zh})
            continue
        manifest = getattr(package, "manifest", None)
        taking.append({
            "workflow_id": str(package.id),
            "name": name,
            "summary": str((manifest or {}).get("summary", "") or "")[:300] if isinstance(manifest, dict) else "",
            "version": _version(package),
            "start_fields": start_fields(package),
            "sites": flow_sites(package),
        })
    return {
        "order_flows": taking,
        "not_taking_orders": not_taking,
        "note": (
            "master_create_order drafts one order on a workflow_id from order_flows, its fields keyed by start_fields ids; "
            "the order waits in 业务工单「待接单」 for the owner: nothing runs, no session, nothing is spent. An order works "
            "only on its flow's sites; another site means changing that step on the builder. When no flow fits, say so "
            "and offer master_propose_flow_draft."
        ),
    }


def clean_fields(package: Any, raw: Any) -> dict[str, Any]:
    """The draft's start fields, checked against the flow's: unknown names and missing required ones are refused.

    A value is text (a number or yes/no is said as text), or a list / object a batch flow takes; an empty value counts
    as not given. Nothing unknown is dropped quietly -- the refusal names it, so the owner's words are never lost.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise DraftRefused("fields_not_object", "开单项要按业务流的开单字段逐项填写", "fields must be an object keyed by the flow's start field ids")
    specs = start_fields(package)
    known = {row["id"]: row for row in specs}
    unknown = sorted({str(key).strip() for key in raw if str(key).strip() not in known})
    if unknown:
        # The owner reads the zh words: labels only, never the field ids or the names the model made up (the model gets
        # both in the en words).
        labels = "、".join(str(row["label"]) for row in specs) or "（这条业务流没有开单项）"
        raise DraftRefused(
            "unknown_fields",
            f"有 {len(unknown)} 项不是这条业务流的开单项。它的开单项是：{labels}",
            "fields not among the flow's start fields: " + ", ".join(unknown[:10])
            + "; its start fields are: " + (", ".join(known) or "(none)"),
        )
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        field_id = str(key).strip()
        if value is None:
            continue
        if isinstance(value, bool):
            value = "是" if value else "否"
        if isinstance(value, (int, float)):
            value = str(value)
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
            if len(value) > MAX_VALUE_CHARS:
                raise DraftRefused("field_too_long", f"「{known[field_id]['label']}」写得太长了", f"field {field_id!r} is longer than {MAX_VALUE_CHARS} characters")
        elif isinstance(value, (list, dict)):
            if not value:
                continue
        else:
            raise DraftRefused("field_not_text", f"「{known[field_id]['label']}」要写成文字", f"field {field_id!r} must be text")
        clean[field_id] = value
    try:
        size = len(json.dumps(clean, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise DraftRefused("field_not_text", "开单项里有写不进工单的内容", f"fields are not plain JSON: {exc}") from exc
    if size > MAX_FIELDS_CHARS:
        raise DraftRefused("fields_too_long", "开单项合起来太长了", f"fields together exceed {MAX_FIELDS_CHARS} characters")
    missing = [row for row in specs if row["required"] and row["id"] not in clean]
    if missing:
        raise DraftRefused(
            "required_fields_missing",
            "还缺必填的开单项：" + "、".join(row["label"] for row in missing) + "。先问清楚再起草",
            "required start fields missing: " + ", ".join(row["id"] for row in missing),
        )
    return clean


def check_draft(packages: list[Any], project_id: str, workflow_id: str, raw_fields: Any,
                adoption_of: AdoptionReader | None) -> tuple[Any, dict[str, Any]]:
    """(the flow, its clean start fields) for a draft this project can place, or :class:`DraftRefused`."""
    if not str(workflow_id or "").strip():
        raise DraftRefused(
            "workflow_required", "要先选一条这个项目能接单的业务流",
            "an order draft needs a workflow_id from master_list_order_flows",
        )
    package = find_package(packages, workflow_id)
    if package is None:
        raise DraftRefused(
            "no_such_flow", "这个项目里没有这条业务流",
            f"no flow {str(workflow_id)!r} here; call master_list_order_flows for the ids",
        )
    refused = not_taking_orders(package, project_id, adoption_of)
    if refused is not None:
        raise refused
    return package, clean_fields(package, raw_fields)
