"""4.9 Q2 leaf: no-flow direct goal through Task / Scope / budget to delivery.

Headless composer, plus a thin master-console adapter (parse_no_flow_master_directive / route_no_flow_master_directive_if_eligible) used by one Q2-WS-B call site in ``_process_master_message``. The leaf itself stays callable without ws_handler. A master-control chat
reply (``interface=master_control``) is not this path: this stamps a work-body
``direct_task`` that refuses order / flow / node / work_item identities, binds
the project direct-task allowance, and records governed fixed output.

Storage failure leaves the task unconfirmed and does not start a consult.
Turn failure keeps the original goal with ``consult_started=False``.

Workspace file policy (Codex 1218 review):
- the name must be a plain relative path that stays inside the resolved
  workspace root (no absolute / drive / UNC paths, no ``..``, no symlink
  escape); otherwise ``no_flow_workspace_name_invalid`` before any write;
- an existing target is never overwritten (``no_flow_workspace_file_exists``);
- the body goes to an exclusive temp file first, the governed delivery is
  recorded, and only then is the temp committed to the target without
  overwrite. Every write / record / save failure goes through
  ``fail_no_flow_goal`` (task BLOCKED, goal kept, ``consult_started=False``).
  Nothing is replayed or rewritten automatically.
- task / session ids are generated per call when the caller gives none.
"""
from __future__ import annotations

from dataclasses import dataclass

import os
import re
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ampuly.core.direct_task_delivery import record_direct_fixed_output
from ampuly.core.direct_task_source import direct_task_source
from ampuly.core.governed_delivery import GovernedDeliveryRefused
from ampuly.core.models import Task, TaskStatus
from ampuly.plugins.office_ui.master_runtime import remember_unfinished_master_goal


FLOW_KEYS = ("order_id", "flow_id", "workflow_id", "node_id", "work_item_id")


def new_no_flow_task_id() -> str:
    return f"no-flow-direct-{uuid.uuid4().hex}"


def new_no_flow_session_id() -> str:
    return f"sess-no-flow-{uuid.uuid4().hex}"


class NoFlowDirectRefused(ValueError):
    """The no-flow direct chain refused before paid work or delivery."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def bind_no_flow_budget(
    *,
    ampuly_home: str | Path,
    project_id: str,
    actor: str,
    customer_id: str = "",
    environment_id: str = "",
) -> str:
    """Open or reuse the project direct-task allowance (order_id empty)."""
    from ampuly.plugins.office_ui.business_store import BusinessStore

    clean_actor = str(actor or "").strip()
    if not clean_actor:
        raise NoFlowDirectRefused("no_flow_actor_missing")
    try:
        store = BusinessStore(Path(ampuly_home))
        budget = store.ensure_direct_task_budget(
            project_id=str(project_id or "").strip(),
            customer_id=str(customer_id or ""),
            environment_id=str(environment_id or ""),
            actor=clean_actor,
        )
    except Exception as exc:
        raise NoFlowDirectRefused("no_flow_budget_unavailable") from exc
    budget_id = str((budget or {}).get("budget_id", "") or "")
    if not budget_id:
        raise NoFlowDirectRefused("no_flow_budget_unavailable")
    if str((budget or {}).get("order_id", "") or "").strip():
        raise NoFlowDirectRefused("no_flow_budget_has_order")
    return budget_id


def build_no_flow_task(
    *,
    project_id: str,
    task_id: str,
    goal: str,
    workspace_root: str | Path,
    budget_id: str,
    customer_id: str = "",
    environment_id: str = "",
    session_id: str = "",
) -> Task:
    """Build one work-body direct task with no flow / order / node keys."""
    workspace = str(workspace_root or "").strip()
    if not workspace:
        raise NoFlowDirectRefused("no_flow_workspace_missing")
    source = direct_task_source(
        kind="direct_task",
        project_id=project_id,
        workspace_root=workspace,
        budget_id=budget_id,
        customer_id=customer_id,
        environment_id=environment_id,
        metadata={"execution_source": "direct_task"},
    )
    metadata = source.to_metadata()
    for key in FLOW_KEYS:
        if str(metadata.get(key, "") or "").strip():
            raise NoFlowDirectRefused("no_flow_business_key_present")
        metadata[key] = ""
    # Explicitly not master_control: chat replies must not look delivered.
    metadata.pop("interface", None)
    return Task(
        id=str(task_id or "").strip() or new_no_flow_task_id(),
        title="no-flow direct goal",
        description=str(goal or ""),
        project_id=str(project_id or "").strip() or "default",
        session_id=str(session_id or "").strip() or new_no_flow_session_id(),
        metadata=metadata,
    )


def assert_no_flow_identity(task: Task) -> None:
    """Refuse if the task smuggled business flow identity."""
    metadata = dict(getattr(task, "metadata", {}) or {})
    for key in FLOW_KEYS:
        if str(metadata.get(key, "") or "").strip():
            raise NoFlowDirectRefused("no_flow_business_key_present")
    if str(metadata.get("execution_source") or "") != "direct_task":
        raise NoFlowDirectRefused("no_flow_execution_source_invalid")
    if str(metadata.get("interface") or "") == "master_control":
        raise NoFlowDirectRefused("no_flow_master_chat_not_delivery")
    if not str(metadata.get("budget_id") or "").strip():
        raise NoFlowDirectRefused("no_flow_budget_missing")
    if not str(metadata.get("workspace_root") or "").strip():
        raise NoFlowDirectRefused("no_flow_workspace_missing")


def resolve_workspace_target(workspace_root: str | Path, name: str) -> Path:
    """Return the absolute target inside the workspace, or refuse.

    Refuses absolute / drive-relative / UNC names, ``..`` segments, NUL, empty
    names, names that resolve outside the workspace (symlink escape), and an
    existing target (no silent overwrite).
    """
    raw = str(name or "")
    if (
        not raw.strip()
        or "\x00" in raw
        or raw != raw.strip()
        or PureWindowsPath(raw).drive
        or PureWindowsPath(raw).root
        or PurePosixPath(raw).is_absolute()
        or ":" in raw
    ):
        raise NoFlowDirectRefused("no_flow_workspace_name_invalid")
    parts = [part for part in raw.replace("\\", "/").split("/")]
    if any(part in ("", ".", "..") for part in parts):
        raise NoFlowDirectRefused("no_flow_workspace_name_invalid")
    root_text = str(workspace_root or "").strip()
    if not root_text:
        raise NoFlowDirectRefused("no_flow_workspace_missing")
    try:
        root = Path(root_text).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise NoFlowDirectRefused("no_flow_workspace_missing") from exc
    if not root.is_dir():
        raise NoFlowDirectRefused("no_flow_workspace_missing")
    target = root.joinpath(*parts)
    try:
        resolved = target.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise NoFlowDirectRefused("no_flow_workspace_name_invalid") from exc
    if resolved == root or root not in resolved.parents:
        raise NoFlowDirectRefused("no_flow_workspace_name_invalid")
    if os.path.lexists(target) or os.path.lexists(resolved):
        raise NoFlowDirectRefused("no_flow_workspace_file_exists")
    return resolved


def _write_temp(target: Path, body: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    with open(temp, "x", encoding="utf-8", newline="") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    return temp


def _commit_temp(temp: Path, target: Path) -> None:
    """Move temp onto target without ever overwriting an existing file."""
    try:
        os.link(temp, target)
    except FileExistsError:
        raise
    except (OSError, NotImplementedError):
        with open(temp, "r", encoding="utf-8", newline="") as src:
            data = src.read()
        with open(target, "x", encoding="utf-8", newline="") as dst:
            dst.write(data)
            dst.flush()
            os.fsync(dst.fileno())
    _discard(temp)


def _discard(path: Path | None) -> None:
    if path is None:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


async def _fail_delivery(
    store: Any,
    task: Task,
    *,
    code: str,
    state: str,
    workspace_path: str = "",
    temp_path: Path | None = None,
) -> dict[str, Any]:
    """One failure exit: BLOCKED, goal kept via ``fail_no_flow_goal``."""
    _discard(temp_path)
    metadata = dict(getattr(task, "metadata", {}) or {})
    metadata["direct_delivery_state"] = state
    metadata["direct_delivery_consult_started"] = False
    metadata["direct_delivered"] = False
    metadata["direct_delivery_failure"] = code
    task.metadata = metadata
    task.status = TaskStatus.BLOCKED
    outcome = await fail_no_flow_goal(
        store, task,
        goal=str(getattr(task, "description", "") or ""),
        reason=code,
    )
    return {
        "ok": False,
        "code": code,
        "consult_started": False,
        "workspace_file": workspace_path,
        "recovery_saved": bool(outcome.get("recovery_saved")),
    }


async def deliver_no_flow_body(
    store: Any,
    task: Task,
    content: str,
    *,
    name: str = "no-flow-output.txt",
    also_write_workspace_file: bool = True,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Record governed fixed output for a no-flow direct task.

    ``attachments`` (the files the goal came with, as master_attachments read them): any file not fully read is
    named at the end of the delivered body, deterministically (4.9-A02).

    Optionally mirrors the body into the workspace as a real file. Storage
    failure marks the task unconfirmed and does not start a consult.
    """
    assert_no_flow_identity(task)
    metadata = dict(getattr(task, "metadata", {}) or {})
    body = str(content or "")
    if not body.strip():
        raise NoFlowDirectRefused("no_flow_content_empty")
    if attachments:
        from ampuly.i18n_runtime import is_zh
        from ampuly.plugins.office_ui.master_attachments import with_unread_disclosure

        body = with_unread_disclosure(body, attachments, zh=is_zh())

    target: Path | None = None
    if also_write_workspace_file:
        target = resolve_workspace_target(metadata.get("workspace_root") or "", name)

    temp: Path | None = None
    if target is not None:
        try:
            temp = _write_temp(target, body)
        except Exception:
            return await _fail_delivery(
                store, task, code="no_flow_workspace_write_failed", state="not_started",
            )

    try:
        ref, handoff_id, recorded = await record_direct_fixed_output(
            store,
            project_id=str(getattr(task, "project_id", "") or ""),
            task_id=str(getattr(task, "id", "") or ""),
            content=body,
            budget_id=str(metadata.get("budget_id") or ""),
            session_id=str(
                getattr(task, "session_id", "")
                or metadata.get("harness_session_id")
                or ""
            ),
            harness_turn_id=str(metadata.get("harness_turn_id") or ""),
            name=name,
            order_id="",
            workflow_id="",
            node_id="",
            work_item_id="",
        )
    except GovernedDeliveryRefused as exc:
        return await _fail_delivery(
            store, task, code=exc.code, state="outcome_unknown", temp_path=temp,
        )
    except Exception:
        return await _fail_delivery(
            store, task, code="no_flow_delivery_unconfirmed",
            state="outcome_unknown", temp_path=temp,
        )

    if ref is None or not recorded:
        return await _fail_delivery(
            store, task, code="no_flow_delivery_unconfirmed",
            state="outcome_unknown", temp_path=temp,
        )

    metadata = dict(getattr(task, "metadata", {}) or {})
    metadata["governed_handoff_id"] = handoff_id
    metadata["direct_output_receipts"] = [ref]
    task.metadata = metadata

    workspace_path = ""
    if target is not None and temp is not None:
        try:
            _commit_temp(temp, target)
        except Exception:
            # Governed record exists but the real file did not land. Keep it
            # BLOCKED / outcome_unknown; no automatic replay or rewrite.
            return await _fail_delivery(
                store, task, code="no_flow_workspace_commit_failed",
                state="outcome_unknown", temp_path=temp,
            )
        workspace_path = str(target)

    metadata = dict(getattr(task, "metadata", {}) or {})
    metadata["direct_delivery_state"] = "recorded"
    metadata["direct_acceptance_state"] = "pending"
    metadata["direct_delivered"] = False
    metadata["direct_delivery_consult_started"] = False
    if workspace_path:
        metadata["no_flow_workspace_file"] = workspace_path
    task.metadata = metadata
    task.status = TaskStatus.DONE
    saver = getattr(store, "save_task", None)
    try:
        if not callable(saver):
            raise RuntimeError("store has no save_task")
        await saver(task)
    except Exception:
        return await _fail_delivery(
            store, task, code="no_flow_task_save_failed",
            state="outcome_unknown", workspace_path=workspace_path,
        )
    return {
        "ok": True,
        "handoff_id": handoff_id,
        "artifact": ref,
        "consult_started": False,
        "workspace_file": workspace_path,
        "budget_id": str(metadata.get("budget_id") or ""),
    }


async def fail_no_flow_goal(
    store: Any,
    task: Task,
    *,
    goal: str,
    reason: str,
) -> dict[str, Any]:
    """Keep the original goal on the task. Does not start a consult.

    Fails closed: ``consult_started`` is True only when the recovery record
    literally says so; missing recovery data reports False.
    """
    assert_no_flow_identity(task)
    try:
        kept = await remember_unfinished_master_goal(
            store, task, goal=goal, reason=reason,
        )
    except Exception:
        kept = False
    metadata = dict(getattr(task, "metadata", {}) or {})
    recovery = metadata.get("master_turn_recovery")
    recovery = dict(recovery) if isinstance(recovery, dict) else {}
    return {
        "ok": bool(kept),
        "recovery_saved": bool(kept),
        "consult_started": recovery.get("consult_started") is True,
        "goal": str(recovery.get("goal") or ""),
        "state": str(recovery.get("state") or ""),
        "reason": str(recovery.get("reason") or ""),
    }


async def run_no_flow_direct_chain(
    *,
    ampuly_home: str | Path,
    store: Any,
    project_id: str,
    actor: str,
    goal: str,
    content: str,
    workspace_root: str | Path,
    task_id: str = "",
    customer_id: str = "",
    environment_id: str = "",
    session_id: str = "",
    name: str = "no-flow-output.txt",
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One leaf-safe chain: budget -> task -> real file + governed delivery.

    Empty ``task_id`` / ``session_id`` get fresh unique ids per call.
    """
    resolve_workspace_target(workspace_root, name)
    budget_id = bind_no_flow_budget(
        ampuly_home=ampuly_home,
        project_id=project_id,
        actor=actor,
        customer_id=customer_id,
        environment_id=environment_id,
    )
    task = build_no_flow_task(
        project_id=project_id,
        task_id=task_id,
        goal=goal,
        workspace_root=workspace_root,
        budget_id=budget_id,
        customer_id=customer_id,
        environment_id=environment_id,
        session_id=session_id,
    )
    assert_no_flow_identity(task)
    saver = getattr(store, "save_task", None)
    if not callable(saver):
        raise NoFlowDirectRefused("no_flow_task_store_missing")
    try:
        await saver(task)
    except Exception:
        delivery = await _fail_delivery(
            store, task, code="no_flow_task_save_failed", state="not_started",
        )
    else:
        delivery = await deliver_no_flow_body(
            store, task, content, name=name, also_write_workspace_file=True,
            attachments=attachments,
        )
    from ampuly.i18n_runtime import is_zh
    from ampuly.plugins.office_ui.master_attachments import unread_attachments

    delivery["unread_attachments"] = unread_attachments(attachments, zh=is_zh())
    delivery["task_id"] = task.id
    delivery["session_id"] = task.session_id
    delivery["budget_id"] = budget_id
    delivery["execution_source"] = "direct_task"
    for key in FLOW_KEYS:
        delivery[key] = ""
    return delivery
 
# ---------------------------------------------------------------------------
# Q2-WS-B: explicit master-console directive -> this leaf (thin adapter)
# ---------------------------------------------------------------------------

NO_FLOW_MASTER_MARKERS_EN = "no-flow-direct:"
NO_FLOW_MASTER_MARKERS_ZH = "无flow直接交付:"
DEFAULT_NO_FLOW_MASTER_NAME = "no-flow-output.txt"
UNLABELED_NO_FLOW_MARKER = "unlabeled-delivery-shaped"

# High-precision unlabeled delivery-shaped first-line templates only.
_UNLABELED_DELIVERY_LINE = (
    re.compile(
        r"^(?:please\s+)?(?:write|save|put|deliver)\s+"
        r"(?:the\s+following\s+)?(?:content\s+)?"
        r"(?:to|into|as)\s+(?:file\s+)?"
        r"(?P<name>[A-Za-z0-9_\-./\u4e00-\u9fff]+\.[A-Za-z0-9]{1,8})"
        r"\s*:?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^save\s+as\s*:\s*"
        r"(?P<name>[A-Za-z0-9_\-./\u4e00-\u9fff]+\.[A-Za-z0-9]{1,8})"
        r"\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:请)?(?:把|将)?以下内容(?:写入|保存到|直接交付到|交付到)(?:文件)?"
        r"\s*(?P<name>[^\s:：]+?\.[A-Za-z0-9]{1,8})\s*[:：]?\s*$"
    ),
    re.compile(
        r"^(?:写入|保存到|直接交付到|交付到)(?:文件)?"
        r"\s*(?P<name>[^\s:：]+?\.[A-Za-z0-9]{1,8})\s*[:：]\s*$"
    ),
)

_QUESTION_PREFIXES = (
    "what ",
    "why ",
    "how ",
    "when ",
    "where ",
    "who ",
    "which ",
    "can you",
    "could you",
    "would you",
    "is ",
    "are ",
    "do ",
    "does ",
    "什么",
    "为什么",
    "怎么",
    "如何",
    "哪",
    "是否",
    "能否",
    "可以吗",
)


@dataclass(frozen=True)
class NoFlowMasterDirective:
    """Parsed no-flow direct-delivery directive from master chat."""

    name: str
    body: str
    goal: str
    marker: str


def _first_content_line_index(lines: list[str]) -> int | None:
    i = 0
    while i < len(lines) and not str(lines[i]).strip():
        i += 1
    return i if i < len(lines) else None


def _goal_from_body(body: str) -> str:
    goal = ""
    for line in str(body or "").splitlines():
        if line.strip():
            goal = line.strip()
            break
    if not goal:
        goal = "no-flow direct delivery"
    if len(goal) > 200:
        goal = goal[:200]
    return goal


def _looks_like_question_line(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    if s.endswith("?") or s.endswith("？"):
        return True
    low = s.lower()
    return any(low.startswith(prefix) for prefix in _QUESTION_PREFIXES)


def _filename_plausible(name: str) -> bool:
    raw = str(name or "")
    if (
        not raw.strip()
        or raw != raw.strip()
        or ".." in raw.replace("\\", "/")
        or PureWindowsPath(raw).drive
        or PureWindowsPath(raw).root
        or PurePosixPath(raw).is_absolute()
        or ":" in raw
        or "\x00" in raw
    ):
        return False
    parts = [part for part in raw.replace("\\", "/").split("/")]
    if any(part in ("", ".", "..") for part in parts):
        return False
    return True


def parse_no_flow_master_directive(content: str) -> NoFlowMasterDirective | None:
    """Return a directive when the bare message uses an explicit marker.

    Ordinary chat (no marker) returns None so the caller falls through to the
    multi-step loop. Leading blank lines are skipped; the marker line may carry
    an optional filename after the colon; the body is every line after that.
    """
    lines = str(content or "").splitlines()
    i = _first_content_line_index(lines)
    if i is None:
        return None
    first = str(lines[i]).strip()
    lower = first.lower()
    marker = ""
    rest = ""
    if lower.startswith(NO_FLOW_MASTER_MARKERS_EN):
        marker = NO_FLOW_MASTER_MARKERS_EN
        rest = first[len(NO_FLOW_MASTER_MARKERS_EN) :].strip()
    elif first.startswith(NO_FLOW_MASTER_MARKERS_ZH):
        marker = NO_FLOW_MASTER_MARKERS_ZH
        rest = first[len(NO_FLOW_MASTER_MARKERS_ZH) :].strip()
    else:
        return None
    name = rest or DEFAULT_NO_FLOW_MASTER_NAME
    body = "\n".join(lines[i + 1 :])
    return NoFlowMasterDirective(
        name=name, body=body, goal=_goal_from_body(body), marker=marker,
    )


def parse_unlabeled_no_flow_delivery(content: str) -> NoFlowMasterDirective | None:
    """High-precision unlabeled delivery-shaped heuristic (no explicit marker).

    Matches only imperative write/save/deliver-to-filename first lines with a
    non-empty body. Chat questions and vague asks return None.
    """
    if parse_no_flow_master_directive(content) is not None:
        return None
    lines = str(content or "").splitlines()
    i = _first_content_line_index(lines)
    if i is None:
        return None
    first = str(lines[i]).strip()
    if _looks_like_question_line(first):
        return None
    name = ""
    for pattern in _UNLABELED_DELIVERY_LINE:
        matched = pattern.match(first)
        if matched:
            name = str(matched.group("name") or "").strip()
            break
    if not name or not _filename_plausible(name):
        return None
    body = "\n".join(lines[i + 1 :])
    if not str(body or "").strip():
        return None
    return NoFlowMasterDirective(
        name=name,
        body=body,
        goal=_goal_from_body(body),
        marker=UNLABELED_NO_FLOW_MARKER,
    )


def parse_no_flow_master_candidate(content: str) -> NoFlowMasterDirective | None:
    """Explicit marker first; else high-precision unlabeled delivery-shaped."""
    return parse_no_flow_master_directive(content) or parse_unlabeled_no_flow_delivery(
        content
    )


def _no_flow_master_refusal(
    *,
    code: str,
    name: str = "",
    goal: str = "",
) -> dict[str, Any]:
    return {
        "ok": False,
        "eligible": True,
        "refused": True,
        "code": code,
        "consult_started": False,
        "name": name,
        "goal": goal,
        "execution_source": "direct_task",
    }


async def route_no_flow_master_directive_if_eligible(
    *,
    content: str,
    ampuly_home: str | Path,
    store: Any,
    project_id: str,
    actor: str,
    workspace_root: str | Path,
    customer_id: str = "",
    environment_id: str = "",
    session_id: str = "",
    message_id: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """If content is an explicit or high-precision unlabeled no-flow directive, run the leaf.

    Returns None when the message is not eligible (caller continues to the
    multi-step loop). A returned dict always means the turn was handled here;
    success or refusal must not fall through to model consult paths.
    """
    directive = parse_no_flow_master_candidate(content)
    if directive is None:
        return None
    if not str(directive.body or "").strip():
        return _no_flow_master_refusal(
            code="no_flow_body_empty", name=directive.name, goal=directive.goal,
        )
    if not str(actor or "").strip():
        return _no_flow_master_refusal(
            code="no_flow_actor_missing", name=directive.name, goal=directive.goal,
        )
    if not str(ampuly_home or "").strip():
        return _no_flow_master_refusal(
            code="no_flow_ampuly_home_missing", name=directive.name, goal=directive.goal,
        )
    if store is None:
        return _no_flow_master_refusal(
            code="no_flow_task_store_missing", name=directive.name, goal=directive.goal,
        )
    if not str(workspace_root or "").strip():
        return _no_flow_master_refusal(
            code="no_flow_workspace_missing", name=directive.name, goal=directive.goal,
        )

    task_id = ""
    mid = str(message_id or "").strip()
    if mid:
        task_id = f"no-flow::{mid}"

    try:
        result = await run_no_flow_direct_chain(
            ampuly_home=ampuly_home,
            store=store,
            project_id=project_id,
            actor=actor,
            goal=directive.goal,
            content=directive.body,
            workspace_root=workspace_root,
            task_id=task_id,
            customer_id=customer_id,
            environment_id=environment_id,
            session_id=session_id,
            name=directive.name,
            attachments=attachments,
        )
    except NoFlowDirectRefused as exc:
        return _no_flow_master_refusal(
            code=str(exc.code or "no_flow_refused"),
            name=directive.name,
            goal=directive.goal,
        )

    out = dict(result)
    out["eligible"] = True
    out["refused"] = not bool(out.get("ok"))
    out["name"] = directive.name
    out["goal"] = directive.goal
    if out.get("refused") and not out.get("code"):
        out["code"] = str(out.get("reason") or out.get("state") or "no_flow_delivery_failed")
    return out

