"""A whole saved canvas flow tried once: every AI step in dependency order, each on what its upstream really left.

A node trial of a step with upstream steps runs on *pinned* results of separate earlier trials, and so is never release
evidence. A flow trial is one run of the saved version instead: the steps go through the same product trial path
(start_node_trial / execute_node_trial, same isolation, same budgeted runtime), and each step is handed exactly the
workspace its upstream trials left *in this run* (chained, not pinned). A run in which every step passed, with no
release assumed for a person, is evidence-grade; a person still decides whether to admit it.

It never invents input: the run stops at the first step that does not succeed, and nothing downstream is tried. It
never spends past its allowance: every step's model-call ceiling is cut to what is left of the run's.

The record lives under ``<ampuly_home>/flow_trials``; state and evidence are derived on every read from the step trials'
own records, never trusted from the file.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ampuly.plugins.office_ui.business_store import BusinessStore, OrderTransitionError
from ampuly.plugins.office_ui.trial_runner import HUMAN_NODE_TYPES
from ampuly.plugins.office_ui.workflow_node_trial import (
    TRIAL_EMPLOYEE_RUNTIME_REQUIRED,
    _descriptor_path,
    _digest,
    _payload,
    _read_json,
    _refuse_other_projects_flow,
    _version,
    _write_json,
    execute_node_trial,
    pin_plan,
    start_node_trial,
    trial_tool_plan,
)
from ampuly.plugins.office_ui.workflow_packages import WorkflowPackage

PREFIX = "flow_trial_"
MAX_NODE_CALLS = 20
MAX_TOTAL_CALLS = 200
# A record derives these on read; the file never holds them.
_DERIVED = frozenset({
    "state", "nodes", "counts_as_evidence", "model_calls_total", "not_run", "stopped_at",
    "phase", "phase_zh", "budget", "budget_refused", "resume_from", "reused",
})
_PHASE_ZH = {
    "accepted": "已受理，这一轮还没跑完",
    "running": "正在执行",
    "waiting": "等你回答；回答后从这里继续，前面的步骤不重做",
    "finished": "这一轮已经结束",
    "unknown": "结果还不能确定，先核验，不要重跑",
}


def _root(ampuly_home: Path) -> Path:
    root = Path(ampuly_home) / "flow_trials"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _record_path(ampuly_home: Path, flow_id: str) -> Path:
    if not flow_id.startswith(PREFIX) or not flow_id[len(PREFIX):].isalnum():
        raise OrderTransitionError("not a flow trial id")
    return _root(ampuly_home) / f"{flow_id}.json"


def flow_plan(package: WorkflowPackage) -> dict[str, Any]:
    """The AI steps in dependency order, which person releases are assumed, or why the flow cannot be tried whole."""
    nodes = [dict(item) for item in package.nodes if isinstance(item, dict)]
    ids = [str(item.get("id", "") or "") for item in nodes]
    by_id = dict(zip(ids, nodes))
    out: dict[str, Any] = {"order": [], "released_by_assumption": [], "blocked": "", "blocked_code": "", "blocked_step": ""}

    def refuse(code: str, step: str, sentence: str) -> dict[str, Any]:
        out.update(blocked=sentence, blocked_code=code, blocked_step=step)
        return out

    # The ordinary trial compiler copies a role, not the employee's frozen
    # runtime basis. Do not count that different execution as employee evidence.
    for node in nodes:
        if "employee_binding" in node:
            return refuse("employee_runtime_trial_required", str(node.get("id", "") or ""),
                          TRIAL_EMPLOYEE_RUNTIME_REQUIRED)

    # Kahn's algorithm, ties broken by the order the canvas saved them in.
    pending = {node_id: [str(dep) for dep in list(by_id[node_id].get("depends_on") or []) if str(dep).strip()] for node_id in ids}
    for node_id, deps in pending.items():
        for dep in deps:
            if dep not in by_id:
                return refuse("unknown_upstream", node_id, f"{node_id!r} depends on {dep!r}, which is not a step of the saved workflow")
    done: list[str] = []
    while len(done) < len(ids):
        ready = [node_id for node_id in ids if node_id not in done and all(dep in done for dep in pending[node_id])]
        if not ready:
            stuck = [node_id for node_id in ids if node_id not in done]
            return refuse("cycle", stuck[0], "the steps " + ", ".join(stuck) + " depend on each other in a circle")
        done.append(ready[0])
    for node_id in done:
        node = by_id[node_id]
        kind = str(node.get("type", "") or "")
        if kind == "ai_do":
            if trial_tool_plan([str(item) for item in list(node.get("tools") or [])])["paid"]:
                return refuse("paid_images", node_id,
                              f"the step {node_id!r} generates images; try it with a node trial, which asks for an image allowance")
            plan = pin_plan(package, node_id)
            if plan["blocked"]:
                return refuse(plan["blocked_code"], plan["blocked_step"], plan["blocked"])
            out["order"].append(node_id)
        elif kind in HUMAN_NODE_TYPES:
            if [item for item in list(node.get("deliverables") or []) if str(item).strip()]:
                return refuse("human_hands_over_files", node_id,
                              f"the human step {node_id!r} hands over files no trial can produce; run the saved workflow instead")
            out["released_by_assumption"].append(node_id)
        else:
            return refuse("upstream_not_pinnable", node_id,
                          f"the step {node_id!r} is a {kind or 'step of an unknown kind'}; run the saved workflow instead")
    if not out["order"]:
        return refuse("no_ai_step", "", "the flow has no AI step to try")
    # Only a release some AI step actually waits behind is assumed.
    behind = {item for node_id in out["order"] for item in pin_plan(package, node_id)["released_by_assumption"]}
    out["released_by_assumption"] = [item for item in out["released_by_assumption"] if item in behind]
    return out


def flow_payload(record: dict[str, Any], business: BusinessStore, ampuly_home: Path) -> dict[str, Any]:
    """The record as read now: every step's state and evidence from that step trial's own record."""
    stored = {key: value for key, value in record.items() if key not in _DERIVED}
    rows: list[dict[str, Any]] = []
    total = 0
    for entry in list(stored.get("steps") or []):
        descriptor = _read_json(_descriptor_path(ampuly_home, str(entry.get("spec_id", "") or "")))
        if descriptor:
            payload = _payload(descriptor, business)
            calls = int(dict(dict(payload.get("budget") or {}).get("consumed") or {}).get("model_call", 0) or 0)
            total += calls
            rows.append({
                "node_id": str(entry.get("node_id", "")), "spec_id": str(entry.get("spec_id", "")),
                "state": str(payload.get("state", "")), "counts_as_evidence": bool(payload.get("counts_as_evidence")),
                "chained_upstream": bool(payload.get("chained_upstream")), "model_calls": calls,
                "artifacts": [str(item.get("path", "")) for item in list(payload.get("artifacts") or []) if isinstance(item, dict)],
            })
        else:
            # A step that was given an id and then lost its record is not a known failure.
            missing = "outcome_unknown" if str(entry.get("spec_id", "") or "") else "failed"
            rows.append({"node_id": str(entry.get("node_id", "")), "spec_id": str(entry.get("spec_id", "")),
                         "state": missing, "counts_as_evidence": False, "chained_upstream": False, "model_calls": 0,
                         "admission_error": str(entry.get("admission_error", "") or "")})
    order = [str(item) for item in list(stored.get("order") or [])]
    tried = [row["node_id"] for row in rows]
    first_bad = next((row for row in rows if row["state"] != "succeeded"), None)
    if first_bad is not None:
        state = {"running": "running", "queued": "running", "waiting_for_answer": "waiting_for_answer"}.get(first_bad["state"], "failed")
        if first_bad["state"] == "abandoned" and stored.get("stop_requested"):
            state = "stopped"
    elif len(tried) < len(order):
        state = "stopped" if stored.get("stop_reason") else ("running" if stored.get("started") else "queued")
    else:
        state = "succeeded"
    budget_refused = "allowance" in str(stored.get("stop_reason") or "")
    if any(row.get("state") == "outcome_unknown" for row in rows):
        phase = "unknown"
    elif state == "waiting_for_answer":
        phase = "waiting"
    elif state in {"succeeded", "failed", "stopped"}:
        phase = "finished"
    elif stored.get("started") or state == "running":
        phase = "running"
    else:
        phase = "accepted"
    per_node = int(stored.get("model_call_limit_per_node", 12) or 12)
    total_limit = stored.get("total_model_call_limit")
    if phase == "unknown":
        sentence = _PHASE_ZH["unknown"]
    elif budget_refused:
        sentence = "整次模型次数已用完，这次停在这里，没有继续花"
    else:
        sentence = _PHASE_ZH[phase]
    stopped_at = first_bad["node_id"] if first_bad is not None else (order[len(tried)] if state == "stopped" else "")
    resume_from = ""
    if phase in {"waiting", "running", "accepted"}:
        done = {row["node_id"] for row in rows if row.get("state") == "succeeded"}
        resume_from = next((item for item in order if item not in done), "")
    return {
        **stored,
        "state": state,
        "phase": phase,
        "phase_zh": sentence,
        "nodes": rows,
        "stopped_at": stopped_at,
        "resume_from": resume_from,
        "not_run": [item for item in order if item not in tried],
        "model_calls_total": total,
        "budget_refused": budget_refused,
        "budget": {
            "per_node": per_node,
            "total": int(total_limit) if total_limit is not None else None,
            "total_source": str(stored.get("total_model_call_limit_source") or ""),
            "consumed": total,
            "refused": budget_refused,
            "sentence_zh": (
                f"每步最多 {per_node} 次，整次最多 {total_limit if total_limit is not None else '未设'} 次。"
                f"已用 {total} 次。"
            ),
        },
        "counts_as_evidence": state == "succeeded" and not stored.get("released_by_assumption")
        and all(row["counts_as_evidence"] for row in rows),
    }


def _pin(record: dict[str, Any]) -> str:
    return str(dict(record.get("freeze") or {}).get("package_fingerprint", "") or "")


def _checked_freeze(freeze: Any, package: WorkflowPackage) -> dict[str, Any]:
    """A run on an enterprise-published employee pin; never a mutable draft digest."""
    if not isinstance(freeze, dict) or freeze.get("freeze_source") != "employee_packages_published" \
            or freeze.get("draft_digest") or len(str(freeze.get("package_fingerprint", "") or "")) != 64 \
            or str(freeze.get("workflow_id", "")) != package.id or str(freeze.get("workflow_version", "")) != _version(package):
        raise OrderTransitionError("flow trial freeze must be an enterprise-published employee pin of this workflow version")
    keys = ("freeze_source", "package_id", "package_version", "package_fingerprint", "index_relative",
            "version_relative", "workflow_id", "workflow_version", "workflow_tree_sha256")
    return {key: str(freeze.get(key, "") or "") for key in keys}


def pinned_root(ampuly_home: Path, freeze: dict[str, Any]) -> Path:
    return (Path(ampuly_home) / "employee_workflow_pins" / str(freeze.get("package_fingerprint", ""))
            / str(freeze.get("workflow_id", ""))).resolve()


def _saved_flows(ampuly_home: Path, *, project_id: str, workflow_id: str, version: str, pin: str = "") -> list[dict[str, Any]]:
    rows = []
    for path in _root(ampuly_home).glob(f"{PREFIX}*.json"):
        record = _read_json(path)
        if str(record.get("project_id", "")) != str(project_id):
            continue
        if str(record.get("workflow_id", "")) != str(workflow_id):
            continue
        if str(record.get("workflow_version", "")) != str(version):
            continue
        if _pin(record) != pin:
            continue
        rows.append(record)
    rows.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return rows


def _with_reused(payload: dict[str, Any], request: str) -> dict[str, Any]:
    if str(payload.get("request_id", "")) != request:
        payload = {**payload, "reused": True}
    return payload


def prepare_flow_trial(
    business: BusinessStore, package: WorkflowPackage, *, ampuly_home: Path, project_id: str, actor: str,
    request_id: str, model_call_limit_per_node: int = 12, total_model_call_limit: int | None = None,
    new_round: bool = False, freeze: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check the whole flow and write its record. Nothing is spent here.

    With ``freeze`` the run is on an enterprise-published employee pin (the
    package must be the copy materialized from that pin); the pin joins the
    run identity, so a draft run and a pinned run never reuse each other.

    The same saved version has one active run. A different click reuses that
    run and its original request id. Another round is an explicit ``new_round``
    after the previous one has finished.
    """
    _refuse_other_projects_flow(package, project_id)
    request = str(request_id or "").strip()[:96]
    if not request:
        raise OrderTransitionError("a flow trial needs a request_id")
    if isinstance(model_call_limit_per_node, bool) or not 1 <= int(model_call_limit_per_node) <= MAX_NODE_CALLS:
        raise OrderTransitionError(f"model_call_limit_per_node must be between 1 and {MAX_NODE_CALLS}")
    if total_model_call_limit is not None and (
            isinstance(total_model_call_limit, bool) or not 1 <= int(total_model_call_limit) <= MAX_TOTAL_CALLS):
        raise OrderTransitionError(f"total_model_call_limit must be between 1 and {MAX_TOTAL_CALLS}")
    if type(new_round) is not bool:
        raise OrderTransitionError("new_round must be true or false")
    if package.status == "retired":
        raise OrderTransitionError("a retired workflow cannot start a flow trial")
    version = _version(package)
    pinned = _checked_freeze(freeze, package) if freeze is not None else None
    if pinned is not None and Path(package.root).resolve() != pinned_root(ampuly_home, pinned):
        raise OrderTransitionError("a pinned flow trial must run the workflow materialized from that published pin")
    pin = pinned["package_fingerprint"] if pinned else ""
    saved = _saved_flows(ampuly_home, project_id=str(project_id), workflow_id=package.id, version=version, pin=pin)
    unfinished = [row for row in saved if flow_payload(row, business, ampuly_home)["phase"] != "finished"]
    if new_round and unfinished and str(unfinished[0].get("request_id", "")) != request:
        raise OrderTransitionError("这一轮还没结束。先查看进度、回答后继续，或停止后再另开一轮。")
    flow_id = PREFIX + _digest([request, str(project_id), package.id, version] + ([pin] if pin else []))[:24]
    path = _record_path(ampuly_home, flow_id)
    if path.exists():
        return _with_reused(flow_payload(_read_json(path), business, ampuly_home), request)
    if not new_round and saved:
        return _with_reused(flow_payload(saved[0], business, ampuly_home), request)
    plan = flow_plan(package)
    if plan["blocked"]:
        raise OrderTransitionError(f"this flow cannot be tried as a whole: {plan['blocked']}")
    explicit_total = total_model_call_limit is not None
    implied_total = min(MAX_TOTAL_CALLS, int(model_call_limit_per_node) * max(len(plan["order"]), 1))
    record = {
        "flow_id": flow_id, "request_id": request, "project_id": str(project_id), "workflow_id": package.id,
        "workflow_version": version, "actor": str(actor or ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "order": list(plan["order"]), "released_by_assumption": list(plan["released_by_assumption"]),
        "model_call_limit_per_node": int(model_call_limit_per_node),
        "total_model_call_limit": int(total_model_call_limit) if explicit_total else implied_total,
        "total_model_call_limit_source": "owner" if explicit_total else "per_node_times_steps",
        "steps": [], "started": False, "launch_claimed": False, "stop_reason": "", "stop_requested": False,
    }
    if pinned is not None:
        record["freeze"] = pinned
    _write_json(path, record)
    return flow_payload(record, business, ampuly_home)


def mark_flow_launch(ampuly_home: Path, flow_id: str) -> bool:
    """Claim the one launch of this run. A second click does not start another."""
    path = _record_path(ampuly_home, flow_id)
    stored = _read_json(path)
    if not stored or stored.get("started") or stored.get("launch_claimed"):
        return False
    stored["launch_claimed"] = True
    _write_json(path, stored)
    return True


def request_flow_stop(business: BusinessStore, ampuly_home: Path, *, project_id: str, flow_id: str, actor: str) -> dict[str, Any]:
    """Ask the run to stop between steps. A step that is waiting is abandoned, not retried."""
    from ampuly.plugins.office_ui.workflow_node_trial import abandon_node_trial

    path = _record_path(ampuly_home, flow_id)
    stored = {key: value for key, value in (_read_json(path) or {}).items() if key not in _DERIVED}
    if not stored or str(stored.get("project_id", "")) != str(project_id):
        raise OrderTransitionError("flow trial not found in this project")
    stored["stop_requested"] = True
    view = flow_payload(stored, business, ampuly_home)
    if view["phase"] == "waiting":
        for row in view["nodes"]:
            if row.get("state") == "waiting_for_answer" and row.get("spec_id"):
                abandon_node_trial(business, ampuly_home=ampuly_home, project_id=str(project_id), spec_id=str(row["spec_id"]), actor=actor)
    if not stored.get("running_now"):
        stored["stop_reason"] = stored.get("stop_reason") or "owner_stopped"
        stored["started"] = True
        stored["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(path, stored)
    return flow_payload(stored, business, ampuly_home)


def _merge_stop_request(path: Path, stored: dict[str, Any]) -> None:
    """A control request made while a node is awaiting must survive executor writes."""
    latest = _read_json(path)
    if not latest or any(latest.get(key) != stored.get(key) for key in ("flow_id", "project_id")):
        raise OrderTransitionError("试跑记录无法核对，已停止继续执行，请先核验原试跑")
    if latest.get("stop_requested"):
        stored["stop_requested"] = True
        stored["stop_reason"] = "owner_stopped"


async def execute_flow_trial(
    business: BusinessStore, package: WorkflowPackage, record: dict[str, Any], *, ampuly_home: Path, actor: str, llm: Any,
    resume: bool = False,
) -> dict[str, Any]:
    """Run the prepared steps; a resume continues the same run and does not redo a step that already passed."""
    flow_id = str(record["flow_id"])
    path = _record_path(ampuly_home, flow_id)
    latest = _read_json(path)
    if not latest or any(latest.get(key) != record.get(key) for key in ("flow_id", "project_id")):
        raise OrderTransitionError("试跑记录无法核对，已停止继续执行，请先核验原试跑")
    stored = {key: value for key, value in latest.items() if key not in _DERIVED}
    view = flow_payload(stored, business, ampuly_home)
    if view["phase"] == "unknown":
        raise OrderTransitionError("这一步的结果还不能确定，先核验原试跑，不能接着跑或另开一轮")
    if view.get("budget_refused") or view["phase"] == "finished":
        return view
    if stored.get("running_now") or (stored.get("started") and not resume) or (view["phase"] == "waiting" and not resume):
        return view
    if stored.get("stop_requested"):
        stored["stop_reason"] = stored.get("stop_reason") or "owner_stopped"
        stored["started"] = True
        stored["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(path, stored)
        return flow_payload(stored, business, ampuly_home)
    if str(stored.get("workflow_version", "")) != _version(package) or str(stored.get("workflow_id", "")) != package.id:
        raise OrderTransitionError("the saved workflow changed after this flow trial was prepared; start a new one")
    if stored.get("freeze") and Path(package.root).resolve() != pinned_root(ampuly_home, dict(stored["freeze"])):
        raise OrderTransitionError("this flow trial is pinned to a published employee package; continue it from that pin")
    stored["started"] = True
    stored["running_now"] = True
    _write_json(path, stored)
    produced = {
        str(row["node_id"]): str(row["spec_id"])
        for row in view["nodes"]
        if row.get("state") == "succeeded" and row.get("spec_id")
    }
    per_node = int(stored.get("model_call_limit_per_node", 12) or 12)
    total_limit = stored.get("total_model_call_limit")
    spent = int(view.get("model_calls_total") or 0)
    execution_error: BaseException | None = None
    try:
        for node_id in list(stored.get("order") or []):
            _merge_stop_request(path, stored)
            if stored.get("stop_requested"):
                stored["stop_reason"] = "owner_stopped"
                break
            if node_id in produced:
                continue
            current = flow_payload(stored, business, ampuly_home)
            existing = next((row for row in current["nodes"] if row.get("node_id") == node_id), None)
            if existing and existing.get("state") == "outcome_unknown":
                raise OrderTransitionError("这一步的结果还不能确定，先核验原试跑，不能接着跑或另开一轮")
            if existing and existing.get("state") == "succeeded" and existing.get("spec_id"):
                produced[node_id] = str(existing["spec_id"])
                spent = int(current.get("model_calls_total") or spent)
                continue
            if existing and existing.get("state") in {"waiting_for_answer", "running", "queued", "failed", "abandoned"}:
                break
            left = None if total_limit is None else int(total_limit) - spent
            if left is not None and left < 1:
                stored["stop_reason"] = "the run's model-call allowance is spent"
                break
            plan = pin_plan(package, node_id)
            pins = [{"node_id": upstream, "spec_id": produced[upstream]} for upstream in plan["required"]]
            entry: dict[str, Any] = {"node_id": node_id, "spec_id": ""}
            try:
                started, spec, claim = await start_node_trial(
                    business, package, node_id, ampuly_home=ampuly_home, project_id=str(stored["project_id"]), actor=actor,
                    request_id=f"{flow_id}:{node_id}", model_call_limit=min(per_node, left) if left is not None else per_node,
                    pins=pins or None, flow={"flow_id": flow_id},
                )
            except OrderTransitionError as exc:
                entry["admission_error"] = str(exc)
                stored["steps"].append(entry)
                break
            entry["spec_id"] = str(started["spec_id"])
            if not any(str(item.get("node_id", "")) == node_id for item in stored["steps"]):
                stored["steps"].append(entry)
            _merge_stop_request(path, stored)
            _write_json(path, stored)
            if stored.get("stop_requested"):
                if claim:
                    # Admission can await upstream verification/claim storage.
                    # A stop in that interval must not start a paid execution.
                    business.finish_builder_round(int(claim["id"]), {
                        "outcome": "unrun", "reason": "flow stopped before node execution",
                        "detail": "", "failed_step": "", "spec_digest": str(claim.get("spec_digest", "")),
                    }, actor=actor)
                    business.close_run_budget(str(started["budget_id"]), actor=actor,
                                              reason="flow stopped before node execution")
                    descriptor_path = _descriptor_path(ampuly_home, str(started["spec_id"]))
                    descriptor = _read_json(descriptor_path)
                    if not descriptor or descriptor.get("project_id") != stored["project_id"]:
                        raise OrderTransitionError("node trial record cannot be verified after stop")
                    descriptor["abandoned"] = {"at": datetime.now(timezone.utc).isoformat(), "by": actor}
                    _write_json(descriptor_path, descriptor)
                break
            finished = await execute_node_trial(
                business, spec, claim, descriptor=started, ampuly_home=ampuly_home, llm=llm, actor=actor,
            ) if claim else started
            spent += int(dict(dict(finished.get("budget") or {}).get("consumed") or {}).get("model_call", 0) or 0)
            _merge_stop_request(path, stored)
            if stored.get("stop_requested"):
                _write_json(path, stored)
                # A running node may have become a question after stop was
                # requested. Close that wait too; do not revive it on continue.
                request_flow_stop(business, ampuly_home, project_id=str(stored["project_id"]), flow_id=flow_id, actor=actor)
                break
            if finished.get("state") != "succeeded":
                break
            produced[node_id] = str(started["spec_id"])
    except BaseException as exc:
        execution_error = exc
        raise
    finally:
        try:
            _merge_stop_request(path, stored)
            stored["running_now"] = False
            final = flow_payload(stored, business, ampuly_home)
            if final["phase"] == "finished" and not stored.get("finished_at"):
                stored["finished_at"] = datetime.now(timezone.utc).isoformat()
            for key in list(_DERIVED):
                stored.pop(key, None)
            _write_json(path, stored)
        except Exception as finalization_error:
            if execution_error is None:
                raise
            # Keep the cause of the interrupted execution, and never repair an
            # unreadable flow record by overwriting it with our stale snapshot.
            execution_error.add_note(f"Flow finalization also failed: {finalization_error}")
    return flow_payload(stored, business, ampuly_home)


async def continue_flow_trial(
    business: BusinessStore, package: WorkflowPackage, *, ampuly_home: Path, project_id: str, flow_id: str, actor: str, llm: Any,
) -> dict[str, Any]:
    """Answer-then-continue: same run, remaining steps only, same budget ledger."""
    record = read_flow_trial(business, ampuly_home, project_id=project_id, flow_id=flow_id)
    if str(record.get("workflow_id", "")) != package.id:
        raise OrderTransitionError("flow trial not found in this project")
    return await execute_flow_trial(business, package, record, ampuly_home=ampuly_home, actor=actor, llm=llm, resume=True)


async def run_flow_trial(
    business: BusinessStore, package: WorkflowPackage, *, ampuly_home: Path, project_id: str, actor: str,
    request_id: str, llm: Any, model_call_limit_per_node: int = 12, total_model_call_limit: int | None = None,
    new_round: bool = False, freeze: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record = prepare_flow_trial(
        business, package, ampuly_home=ampuly_home, project_id=project_id, actor=actor, request_id=request_id,
        model_call_limit_per_node=model_call_limit_per_node, total_model_call_limit=total_model_call_limit,
        new_round=new_round, freeze=freeze,
    )
    return await execute_flow_trial(business, package, record, ampuly_home=ampuly_home, actor=actor, llm=llm)


def read_flow_trial(business: BusinessStore, ampuly_home: Path, *, project_id: str, flow_id: str) -> dict[str, Any]:
    record = _read_json(_record_path(ampuly_home, flow_id))
    if not record or str(record.get("project_id", "")) != str(project_id):
        raise OrderTransitionError("flow trial not found in this project")
    return flow_payload(record, business, ampuly_home)


def list_flow_trials(
    business: BusinessStore, ampuly_home: Path, *, project_id: str, workflow_id: str = "",
) -> list[dict[str, Any]]:
    out = []
    for path in _root(ampuly_home).glob(f"{PREFIX}*.json"):
        record = _read_json(path)
        if str(record.get("project_id", "")) != str(project_id):
            continue
        if workflow_id and str(record.get("workflow_id", "")) != str(workflow_id):
            continue
        out.append(flow_payload(record, business, ampuly_home))
    out.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return out[:50]
