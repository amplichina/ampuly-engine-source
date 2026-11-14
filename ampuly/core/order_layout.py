"""Where an order's files live (data phase 1, docs/DATA-PHASE1-SPEC-2026-09-22.md §2).

    P = get_project_workplace(project)
      orders/<order_id>/            O: the order's workspace root (the business number, wo-YYYYMMDD-xxxxxx)
        output/                     OUT: the flow's one shared delivery tree; each step owns its declared ``writes``
        steps/<step>/downloads/     what the browser saved for this step (written by the runtime, read-only to models)
        steps/<step>/work/          this step's scratch files (written by this step only)
        inputs/                     reserved; nothing fills it in phase 1
        audit/                      browser audit exports
        .order-steps.json           the step table frozen at launch
      delivery-history/<order_id>/  delivery-exports/<order_id>/  portfolio-exports/
      orders/<task-id>/browser-downloads|audit/     legacy, read only

The new areas sit beside ``output/`` rather than in it, so no flow's own paths can collide with them and nothing that
collects a delivery (evidence, acceptance, the gate) ever picks them up; a step still reads them as ``../steps/…``
because its file tools are bounded by O. Names on disk are ASCII; the console shows ``DISPLAY_NAMES``.

Pure: nothing here writes to the disk; resolving a path is the only thing that looks at it.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

ORDERS = "orders"
OUTPUT = "output"
STEPS = "steps"
DOWNLOADS = "downloads"
WORK = "work"
INPUTS = "inputs"
AUDIT = "audit"
STEP_TABLE = ".order-steps.json"
UNASSIGNED = "_unassigned"
LEGACY_DOWNLOADS = "browser-downloads"
# The project's own material, shared by its orders: what the customer gave, brand assets, uploads. An order reads
# ``common/`` and its own customer's folder. The separate name avoids collisions with a workflow's
# ``materials/<item>/`` outputs; a plain session's workspace root is the project itself.
PROJECT_MATERIALS = "project-materials"
MATERIALS_COMMON = "common"
MATERIALS_CUSTOMERS = "customers"
DELIVERY_HISTORY = "delivery-history"
DELIVERY_EXPORTS = "delivery-exports"
PORTFOLIO_EXPORTS = "portfolio-exports"

# Names at order level that belong to the runtime, never to a step's own writes.
RESERVED_ORDER_NAMES = (STEPS, INPUTS, AUDIT, STEP_TABLE)
# Project-level folders whose first segment below them is a business order.
ORDER_KEYED_ROOTS = (ORDERS, DELIVERY_HISTORY, DELIVERY_EXPORTS)

DISPLAY_NAMES = {
    OUTPUT: "交付",
    STEPS: "步骤",
    DOWNLOADS: "下载",
    WORK: "工作",
    INPUTS: "输入",
    AUDIT: "操作记录",
    LEGACY_DOWNLOADS: "下载（旧）",
    DELIVERY_HISTORY: "验收历史",
    DELIVERY_EXPORTS: "导出",
    PORTFOLIO_EXPORTS: "导出",
    PROJECT_MATERIALS: "资料",
}

# A customer id that may be a folder name as it is: lower case, and nothing a file system argues about. Upper case is
# not allowed through, so ``Acme`` and ``acme`` can never share one folder on a disk that ignores case.
_PLAIN_CUSTOMER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
def customer_segment(customer_id: str) -> str:
    """The folder name a customer's material sits under: the id itself when it is safe, otherwise a digest of it.

    "" for no customer. A digest form starts with ``_``, which a plain id never does, so the two cannot collide.
    """
    clean = str(customer_id or "").strip()
    if not clean:
        return ""
    if _PLAIN_CUSTOMER.fullmatch(clean) and not clean.endswith(".") and not _device_name(clean):
        return clean
    return "_id-" + hashlib.sha256(clean.encode("utf-8")).hexdigest()[:12]

# The refusals a write can get from the order fence (tool_hooks reports them as the ownership outcome).
OUTSIDE_STEP_AREA = "outside_step_area"
OUTSIDE_NODE_WRITES = "outside_node_writes"

_ORDER_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_STEP_SEGMENT = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_TASK_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_DEVICE_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))})


def _device_name(text: str) -> bool:
    # Windows reserves the name with any extension as well: "con.txt" is the console.
    return text.split(".", 1)[0].upper() in _DEVICE_NAMES


def order_segment_ok(text: Any) -> bool:
    """Whether ``text`` can be an order's folder name: one ASCII segment, no trailing dot, no device name."""
    value = str(text or "")
    return bool(_ORDER_SEGMENT.fullmatch(value)) and not value.endswith(".") and not _device_name(value)


def step_segment(node_id: Any) -> str:
    """The folder name of a step: its node id when that is a plain lowercase id, otherwise a digest of it.

    The digest form starts with ``_`` and a plain id never does, so the two cannot collide; ``_unassigned`` is the
    folder of a task that is on no step.
    """
    value = str(node_id or "")
    if not value.strip():
        return UNASSIGNED
    if _STEP_SEGMENT.fullmatch(value) and not _device_name(value):
        return value
    return "_id-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _absolute(value: Any) -> Path | None:
    try:
        return Path(os.path.abspath(os.path.normpath(os.path.expanduser(str(value)))))
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _resolved(value: Any) -> Path | None:
    try:
        return Path(os.path.expanduser(str(value))).resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _parts_under(path: Any, root: Any) -> tuple[str, ...] | None:
    """``path``'s components below ``root`` (lexically, compared the way the OS compares names); None when outside.

    ``root`` is tried as written and as resolved, so a path copied from task metadata (always resolved) and one built
    from the project workplace (possibly not) are both recognised.
    """
    candidate = _absolute(path)
    if candidate is None:
        return None
    bases = []
    for base in (_absolute(root), _resolved(root)):
        if base is not None and base not in bases:
            bases.append(base)
    for base in bases:
        try:
            common = os.path.commonpath((str(base), str(candidate)))
        except ValueError:
            continue
        if os.path.normcase(common) != os.path.normcase(str(base)):
            continue
        relative = os.path.relpath(str(candidate), str(base))
        return () if relative == "." else tuple(Path(relative).parts)
    return None


@dataclass(frozen=True)
class OrderArea:
    """One order's folders as one step sees them. Built by ``order_area_of`` only."""

    project_id: str
    order_id: str
    order_root: Path
    output_root: Path
    step: str

    @property
    def project_root(self) -> Path:
        return self.order_root.parent.parent

    @property
    def step_root(self) -> Path:
        return self.order_root / STEPS / self.step

    @property
    def downloads(self) -> Path:
        return self.step_root / DOWNLOADS

    @property
    def work(self) -> Path:
        return self.step_root / WORK

    @property
    def audit(self) -> Path:
        return self.order_root / AUDIT

    @property
    def inputs(self) -> Path:
        return self.order_root / INPUTS

    @property
    def step_table(self) -> Path:
        return self.order_root / STEP_TABLE

    def p_relative(self, path: Any) -> str | None:
        """``path`` as a POSIX path relative to the project workplace; None when it is not inside it."""
        parts = _parts_under(path, self.project_root)
        return None if parts is None else "/".join(parts)

    def read_as(self, path: Any) -> str | None:
        """How the step's file tools name ``path`` (relative to the output root, e.g. ``../steps/x/downloads/f.csv``);
        None when it is not inside this order."""
        parts = _parts_under(path, self.order_root)
        if parts is None:
            return None
        output = _parts_under(self.output_root, self.order_root) or ()
        shared = 0
        while shared < min(len(parts), len(output)) and os.path.normcase(parts[shared]) == os.path.normcase(output[shared]):
            shared += 1
        return "/".join([".."] * (len(output) - shared) + list(parts[shared:])) or "."


def order_area_of(
    project_id: Any,
    metadata: Mapping[str, Any] | None,
    *,
    roots: Iterable[Any] | None = None,
    check_order_id: bool = True,
    workplace: Any = None,
) -> OrderArea | None:
    """The order area a task works in, or None when it works in no single order.

    ``roots`` is ``(workspace_root, output_root)``; by default the task's durable top-level roots
    (``workspace_root``/``comms_workspace_root``, ``output_root``/``target_output_dir``) -- never the in-memory
    ``_execution_context``, which for a worktree subagent is a temporary folder. An area exists only when the workspace
    root is one folder directly under the project's ``orders/``, its name is a valid order segment, the output root is
    strictly inside it (and not in one of the runtime's areas), and -- when ``check_order_id`` -- the task's
    ``order_id`` is empty or names that folder. The paths are rebuilt from the project workplace, never taken from the
    metadata string, so a lexical containment check against the workplace still holds.
    """
    from ampuly.project_id import is_valid_project_id

    meta = dict(metadata or {})
    if roots is None:
        workspace = str(meta.get("workspace_root", "") or meta.get("comms_workspace_root", "") or "").strip()
        output = str(meta.get("output_root", "") or meta.get("target_output_dir", "") or "").strip()
    else:
        workspace, output = (str(value or "").strip() for value in tuple(roots)[:2])
    if not workspace or not output:
        return None
    project = str(project_id or "").strip() or "default"
    if not is_valid_project_id(project):
        return None
    if workplace is None:
        # Looked up when called: tests patch ampuly.core.config.get_project_workplace.
        from ampuly.core.config import get_project_workplace

        try:
            workplace = get_project_workplace(project)
        except Exception:
            return None
    project_root = Path(workplace)
    orders = _resolved(project_root / ORDERS)
    workspace_path = _resolved(workspace)
    output_path = _resolved(output)
    if orders is None or workspace_path is None or output_path is None:
        return None
    if os.path.normcase(str(workspace_path.parent)) != os.path.normcase(str(orders)):
        return None
    segment = workspace_path.name
    if not order_segment_ok(segment):
        return None
    below = _parts_under(output_path, workspace_path)
    if not below or os.path.normcase(below[0]) in {os.path.normcase(name) for name in RESERVED_ORDER_NAMES}:
        return None
    order_id = str(meta.get("order_id", "") or "").strip()
    if check_order_id and order_id and os.path.normcase(order_id) != os.path.normcase(segment):
        return None
    node = meta.get("workflow_node")
    node_id = node.get("id", "") if isinstance(node, Mapping) else ""
    order_root = project_root / ORDERS / segment
    return OrderArea(
        project_id=project,
        order_id=segment,
        order_root=order_root,
        output_root=order_root.joinpath(*below),
        step=step_segment(node_id),
    )


def order_write_decision(area: OrderArea, raw_paths: Iterable[Any], write_roots: Iterable[str]) -> str | None:
    """Whether a step may write all of ``raw_paths``: None, or the refusal (``OUTSIDE_STEP_AREA`` /
    ``OUTSIDE_NODE_WRITES``).

    An allow-list, compared one component at a time the way the OS compares names, so a case variant cannot slip
    past it: the step's own ``steps/<step>/work/`` (any file below it); otherwise only under the output root, and
    there under the node's declared ``writes`` when it has any. Relative paths are taken from the output root, as the
    file tools and the ownership store take them.
    """
    from ampuly.layer2_organization.node_contract import path_allowed_by_node

    roots = [str(root) for root in write_roots or () if str(root)]
    normcase = os.path.normcase
    reserved = {normcase(name) for name in RESERVED_ORDER_NAMES}
    output = _parts_under(area.output_root, area.order_root) or ()
    output_folded = [normcase(part) for part in output]
    for raw in raw_paths:
        text = str(raw or "")
        try:
            candidate = Path(os.path.expanduser(text))
            absolute = candidate.is_absolute()
            joined = text if absolute else str(area.output_root / candidate)
        except (OSError, RuntimeError, TypeError, ValueError):
            return OUTSIDE_STEP_AREA
        parts = _parts_under(joined, area.order_root)
        if not parts:
            return OUTSIDE_STEP_AREA
        folded = [normcase(part) for part in parts]
        if len(parts) >= 4 and folded[:3] == [normcase(STEPS), normcase(area.step), normcase(WORK)]:
            continue
        if folded[0] in reserved:
            return OUTSIDE_STEP_AREA
        if not output or len(parts) <= len(output) or folded[: len(output)] != output_folded:
            return OUTSIDE_STEP_AREA
        if roots:
            # A relative path is judged as written (``..`` stays refused); an absolute one by its place below OUT.
            probe = "/".join(parts[len(output):]) if absolute else text
            if not path_allowed_by_node(probe, roots):
                return OUTSIDE_NODE_WRITES
    return None


def classify(p_relative: Any) -> dict[str, Any]:
    """What a project-relative path is: ``{order_id, area, step, kind, legacy}``.

    ``area`` is one of ``output``, ``steps``, ``audit``, ``inputs``, ``step_table``, ``order`` (anything else in an
    order folder), ``downloads`` (the legacy task-id ``browser-downloads``), ``delivery-history``,
    ``delivery-exports``, ``portfolio-exports``, ``project-materials``, or ``""``. ``order_id`` is the folder under an
    order-keyed root -- for a legacy folder that is a task id, which no business order carries, so a lookup of it
    finds nothing. ``customer`` is filled in only for the project's material area: ``common``, a customer's segment,
    or ``""``.
    """
    result: dict[str, Any] = {"order_id": "", "area": "", "step": "", "kind": "", "legacy": False, "customer": ""}
    parts = [part for part in str(p_relative or "").replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return result
    head = parts[0].casefold()
    if head == PROJECT_MATERIALS:
        # The project's own material: shared (``common``) or one customer's. Anything else directly under it belongs
        # to the area but to no customer.
        result["area"] = PROJECT_MATERIALS
        below = parts[1].casefold() if len(parts) >= 2 else ""
        if below == MATERIALS_COMMON:
            result["customer"] = MATERIALS_COMMON
        elif below == MATERIALS_CUSTOMERS:
            result["customer"] = parts[2] if len(parts) >= 3 else ""
        return result
    if head == PORTFOLIO_EXPORTS:
        result["area"] = PORTFOLIO_EXPORTS
        return result
    if head in (DELIVERY_HISTORY, DELIVERY_EXPORTS):
        result["area"] = head
        result["order_id"] = parts[1] if len(parts) >= 2 else ""
        return result
    if head != ORDERS or len(parts) < 2:
        return result
    folder = parts[1]
    result["order_id"] = folder
    sub = parts[2].casefold() if len(parts) >= 3 else ""
    if sub == OUTPUT:
        result["area"] = OUTPUT
    elif sub == STEPS:
        result["area"] = STEPS
        result["step"] = parts[3] if len(parts) >= 4 else ""
        result["kind"] = parts[4].casefold() if len(parts) >= 5 else ""
    elif sub == AUDIT:
        result["area"] = AUDIT
        result["legacy"] = bool(_TASK_ID.fullmatch(folder))
    elif sub == INPUTS:
        result["area"] = INPUTS
    elif sub == LEGACY_DOWNLOADS:
        result["area"] = DOWNLOADS
        result["legacy"] = True
    elif sub == STEP_TABLE:
        result["area"] = "step_table"
    else:
        result["area"] = "order"
    return result
