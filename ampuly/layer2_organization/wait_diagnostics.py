"""Bounded, read-only diagnostics from a controller's already-loaded snapshot.

No Store, lock, model, clock or dispatcher is consulted. A cycle is a candidate
in the observed wait graph, not proof that the run failed or must be unparked.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import Any

from ampuly.core.models import Phase, TaskStatus
from ampuly.layer2_organization.communication import CommunicationManager
from ampuly.layer2_organization.company_runtime_identity import is_runtime_auxiliary_task
from ampuly.layer2_organization.phase import task_status_for_phase
from ampuly.layer2_organization.work_item_identity import projection_id_for_task
from ampuly.layer2_organization.work_item_links import linked_work_item_id_for_task

MAX_SNAPSHOT_TASKS = 512
MAX_WAIT_TARGETS = 128
MAX_WAIT_EDGES = 2048
MAX_REPORTED_CYCLE_EDGES = 64
_TERMINAL = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
_HUMAN = {TaskStatus.AWAITING_HUMAN, TaskStatus.AWAITING_REVIEW, TaskStatus.AWAITING_MANAGER_REVIEW}


def _id(value: Any) -> bool:
    return (
        type(value) is str and 0 < len(value) <= 256 and value == value.strip()
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _ids(value: Any, limit: int = MAX_WAIT_TARGETS) -> bool:
    return type(value) in (list, tuple, set, frozenset) and len(value) <= limit and all(_id(item) for item in value)


def diagnose_wait_snapshot(
    tasks: list[Any],
    work_items: list[Any],
    *,
    project_id: str,
    runtime_session_id: str,
    run_id: str,
    busy_task_ids: list[str] | set[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Diagnose one explicit project/root-session/run, without loading anything.

    ``work_items`` must be the same controller snapshot as ``tasks``. Structured
    Task links, not legacy metadata, select authoritative WorkItems. In-flight
    tasks cannot close a wait cycle. Ambiguous roles/links are observations of
    missing evidence, never permission to select a candidate arbitrarily.
    """
    scope = {"project_id": project_id, "runtime_session_id": runtime_session_id, "run_id": run_id}
    result: dict[str, Any] = {
        "scope": scope, "read_only": True, "status": "observed",
        "candidate_cycle_edges": [], "wait_reason_counts": {}, "notes": [],
        "scoped_task_count": 0, "cycle_edges_truncated": False,
    }

    def finish() -> dict[str, Any]:
        canonical = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        result["fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return result

    def invalid(code: str) -> dict[str, Any]:
        result["status"] = "invalid"
        result["notes"] = [{"code": code}]
        result["candidate_cycle_edges"] = []
        return finish()

    if not all(_id(value) for value in scope.values()):
        # Do not echo malformed identifiers or payloads in a diagnostic.
        result["scope"] = {}
        return invalid("scope_invalid")
    if (
        type(tasks) is not list or type(work_items) is not list
        or len(tasks) > MAX_SNAPSHOT_TASKS or len(work_items) > MAX_SNAPSHOT_TASKS
        or not _ids(busy_task_ids, MAX_SNAPSHOT_TASKS)
    ):
        return invalid("snapshot_limit_or_shape_invalid")

    notes: set[tuple[str, str]] = set()

    def note(code: str, task_id: str = "") -> None:
        notes.add((code, task_id))

    item_by_id: dict[str, Any] = {}
    for item in work_items:
        if getattr(item, "run_id", None) != run_id:
            continue
        wid = getattr(item, "work_item_id", None)
        if not _id(wid) or wid in item_by_id:
            return invalid("work_item_identity_invalid")
        item_by_id[wid] = item

    task_by_id: dict[str, Any] = {}
    for task in tasks:
        metadata = getattr(task, "metadata", None)
        if type(metadata) is not dict:
            note("task_metadata_invalid")
            continue
        if (
            getattr(task, "project_id", None) != project_id
            or metadata.get("delegation_run_id") != run_id
            or (getattr(task, "parent_session_id", None) or metadata.get("company_runtime_root_session_id") or metadata.get("parent_session_id") or getattr(task, "session_id", None)) != runtime_session_id
        ):
            # Do not expose the identities of another project/runtime.
            continue
        if is_runtime_auxiliary_task(task):
            continue
        tid = getattr(task, "id", None)
        if not _id(tid) or tid in task_by_id:
            return invalid("task_identity_invalid")
        task_by_id[tid] = task
    result["scoped_task_count"] = len(task_by_id)
    busy = set(busy_task_ids) & task_by_id.keys()

    by_role: dict[str, list[str]] = defaultdict(list)
    by_alias: dict[str, set[str]] = defaultdict(set)
    by_work_item: dict[str, set[str]] = defaultdict(set)
    task_items: dict[str, Any] = {}
    observed_statuses: dict[str, TaskStatus] = {}
    reasons: Counter[str] = Counter()
    eligible: set[str] = set()
    for tid, task in task_by_id.items():
        wid = linked_work_item_id_for_task(task)
        item = item_by_id.get(wid) if wid else None
        if wid and item is None:
            note("work_item_missing", tid)
            continue
        try:
            status = task_status_for_phase(item.phase) if item is not None else TaskStatus(task.status)
        except (KeyError, TypeError, ValueError):
            note("status_invalid", tid)
            continue
        if status in _TERMINAL:
            continue
        observed_statuses[tid] = status
        if item is not None:
            task_items[tid] = item
            role = item.role_id
            by_work_item[wid].add(tid)
        else:
            role = task.assigned_to or task.metadata.get("work_item_role_id", "")
        if role and not _id(role):
            note("role_invalid", tid)
            continue
        if role:
            by_role[role].append(tid)
        by_alias[tid].add(tid)
        projection = projection_id_for_task(task)
        if _id(projection):
            by_alias[projection].add(tid)
        if tid in busy or status == TaskStatus.RUNNING:
            reasons["in_flight"] += 1
        elif task.metadata.get("dispatch_hold") or task.metadata.get("company_runtime_suspend_checkpoint_id"):
            reasons["dispatch_held"] += 1
        elif status in _HUMAN:
            reasons["human_wait"] += 1
        elif status == TaskStatus.AWAITING_PEER:
            reasons["peer_wait"] += 1
            eligible.add(tid)
        elif item is not None and item.phase == Phase.WAITING_DEPENDENCIES:
            reasons["dependency_wait"] += 1
            eligible.add(tid)
        elif item is None and status == TaskStatus.PENDING:
            reasons["pending"] += 1
            eligible.add(tid)
        elif status == TaskStatus.BLOCKED:
            reasons["blocked_other"] += 1

    graph: dict[str, set[str]] = {}
    edge_count = 0
    for tid in sorted(eligible):
        task = task_by_id[tid]
        item = task_items.get(tid)
        targets: set[str] = set()
        if item is not None and len(by_work_item[item.work_item_id]) != 1:
            note("work_item_link_ambiguous", tid)
            continue
        if observed_statuses[tid] == TaskStatus.AWAITING_PEER:
            wait = task.metadata.get("peer_wait", {})
            if type(wait) is not dict:
                note("peer_wait_invalid", tid)
                continue
            kind = wait.get("kind")
            if kind == "comms_blocking":
                field = "awaiting_replies_from"
            elif kind == "peer_message":
                if wait.get("live_required") is not True:
                    note("legacy_nonblocking_peer_wait", tid)
                    continue
                field = "waiting_on_agents"
            elif kind == "async_mailbox":
                note("legacy_nonblocking_peer_wait", tid)
                continue
            else:
                note("peer_wait_kind_unverified", tid)
                continue
            raw = wait.get(field, [])
            if not _ids(raw):
                note("peer_targets_invalid", tid)
                continue
            if not raw:
                note("peer_targets_missing", tid)
            for role in sorted(set(raw)):
                candidates = by_role.get(role, [])
                if len(candidates) != 1:
                    note("peer_role_ambiguous" if candidates else "peer_role_unobserved", tid)
                else:
                    targets.add(candidates[0])
        else:
            if item is not None:
                metadata = getattr(item, "metadata", None)
                raw = metadata.get("dependency_work_item_ids", []) if type(metadata) is dict else None
            else:
                raw = task.dependencies
            if not _ids(raw):
                note("dependencies_invalid", tid)
                continue
            for dep in sorted(set(raw)):
                candidates = (by_work_item if item is not None else by_alias).get(dep, set())
                if len(candidates) != 1:
                    note("dependency_ambiguous" if candidates else "dependency_unobserved", tid)
                else:
                    targets.update(candidates)
        # In-flight, held, human or otherwise unknown nodes do not close a ring.
        targets &= eligible
        if targets:
            graph[tid] = targets
            edge_count += len(targets)
            if edge_count > MAX_WAIT_EDGES:
                return invalid("wait_graph_limit")

    # Reuse the existing iterative detector. Sorted tuples make its traversal
    # deterministic; the implementation consumes neighbor iterables, not sets.
    ordered_graph = {key: tuple(sorted(graph[key])) for key in sorted(graph)}
    pairs = sorted(CommunicationManager._find_wait_cycles(ordered_graph))
    result["candidate_cycle_edges"] = [list(pair) for pair in pairs[:MAX_REPORTED_CYCLE_EDGES]]
    result["cycle_edges_truncated"] = len(pairs) > MAX_REPORTED_CYCLE_EDGES
    result["wait_reason_counts"] = dict(sorted(reasons.items()))
    result["notes"] = [
        {"code": code, **({"task_id": tid} if tid else {})}
        for code, tid in sorted(notes)
    ]
    if notes or result["cycle_edges_truncated"]:
        result["status"] = "incomplete"
    return finish()


def wait_diagnostic_message(
    diagnostic: dict[str, Any], *, include_wait_reasons: bool = False,
) -> str | None:
    """A bounded chat notice, deliberately not the legacy failed/deadlock type.

    Use only results produced above. Route with a server-selected scoped task_id
    to the normal progress callback; do not let a client choose scope or text.
    """
    pairs = diagnostic.get("candidate_cycle_edges", [])
    if not pairs:
        if not include_wait_reasons:
            return None
        labels = {
            "in_flight": "在途", "dispatch_held": "已挂起", "human_wait": "等人",
            "peer_wait": "等待同伴", "dependency_wait": "等待依赖",
            "pending": "待执行", "blocked_other": "其他阻塞",
        }
        counts = diagnostic.get("wait_reason_counts", {})
        summary = "、".join(f"{label} {counts[code]}" for code, label in labels.items() if counts.get(code))
        unavailable = diagnostic.get("status") == "invalid"
        evidence = "诊断不可用" if unavailable else "当前快照未证实等待环，不代表已排除"
        partial = "；证据不完整" if diagnostic.get("status") == "incomplete" else ""
        return (
            f"[RuntimeDiagnostic] 运行停放诊断：{summary or '无可核实等待项'}；{evidence}{partial}。"
            "仅为只读观察，未改变状态、释放锁或自动重派。"
        )
    ids = sorted({tid for pair in pairs for tid in pair})
    visible = ", ".join(tid[:48] for tid in ids[:8])
    more = "…" if len(ids) > 8 or diagnostic.get("cycle_edges_truncated") else ""
    partial = "；快照证据不完整" if diagnostic.get("status") == "incomplete" else ""
    return (
        f"[RuntimeDiagnostic] 疑似循环等待：{visible}{more}{partial}。"
        "仅为当前运行快照的只读诊断，未改变状态、释放锁或自动重派；请人工核查等待关系。"
    )
