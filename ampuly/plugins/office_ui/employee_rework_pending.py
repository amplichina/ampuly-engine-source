"""Read exact retained child-review records through the already-owned runtime.

No lazy project creation, controller admission, dispatch, schema repair or model
call occurs here. A displayed candidate is not permission: confirmation still
rechecks the current owner, original record, attempt, budget and Store fence.
"""
from __future__ import annotations

from ampuly.layer2_organization.human_review_rework import (
    ACTION, ReworkScope, HumanReviewReworkError, _id, _fail,
    build_pending_record, validate_confirm_request, prepare_confirmation,
    resolve_rework_branch,
)
from ampuly.layer2_organization.human_review_rework_executor import _read_graph
from ampuly.layer2_organization.human_review_employee_basis import verified_retained_employee_run

PENDING_ACTION = "human_review_rework_pending"
LOOKUP_ACTION = "human_review_rework_lookup"
MAX_PENDING = 32


def _pending_request(data):
    keys = {"type", "project_id", "order_id", "task_id", "request_id"}
    if type(data) is not dict or set(data) != keys or data.get("type") != PENDING_ACTION:
        _fail()
    for key in keys - {"type"}:
        _id(data[key])
    return dict(data)


async def _existing_context(handler, ws, request):
    from .ws_handler import (_socket_behind, _socket_principal_state,
        _socket_principal_entry, _current_socket_principal)
    from .business_store import ENDED_ORDER_STATUSES

    state = _socket_principal_state(handler)
    account_store, manager, entries = state
    raw = _socket_behind(ws)
    entry = _socket_principal_entry(entries, raw)
    if entry is None:
        _fail("rework_forbidden")
    binding = entry[1]
    actor = _current_socket_principal(handler, raw, expected_binding=binding)
    if actor is None or actor.role != "owner" or actor.disabled:
        _fail("rework_forbidden")
    login, business = actor.login, account_store.business

    async def recheck():
        if (_socket_principal_state(handler) is not state
                or _socket_principal_entry(entries, raw) is not entry
                or manager._store is not account_store or account_store.business is not business):
            _fail("rework_forbidden")
        current = _current_socket_principal(handler, raw, expected_binding=binding)
        if current is None or current.role != "owner" or current.disabled or current.login != login:
            _fail("rework_forbidden")
        order = business.get_order(request["order_id"], project_id=request["project_id"])
        if (order is None or order.project_id != request["project_id"]
                or order.launched_task_id != request["task_id"]):
            _fail("rework_stale")
        return order

    order = await recheck()
    root = vars(handler).get("_root_engine")
    if root is None:
        _fail("rework_source_unavailable")
    project = request["project_id"]
    if vars(root).get("project_id") == project:
        engine = root
    else:
        delegates = vars(root).get("_project_engine_delegates")
        engine = delegates.get(project) if type(delegates) is dict else None
    if engine is None or vars(engine).get("project_id") != project:
        _fail("rework_source_unavailable")
    store = vars(engine).get("store")
    executor = vars(engine).get("company_executor")
    if (store is None or vars(store).get("_db") is None
            or vars(store).get("project_id") != project or executor is None
            or executor.store is not store):
        _fail("rework_source_unavailable")
    target = await handler._resolve_company_runtime_target(request["task_id"], engine=engine)
    if type(target) is not dict:
        _fail("rework_source_unavailable")
    config = target.get("config_task") or target.get("task")
    run_id = (getattr(config, "metadata", {}) or {}).get("delegation_run_id")
    session = target.get("runtime_session_id")
    _id(run_id)
    _id(session)
    snapshot = await engine._load_company_runtime_snapshot(session, delegation_run_id=run_id)
    if snapshot is None:
        _fail("rework_source_unavailable")
    await recheck()
    return engine, executor, snapshot, run_id, order, recheck, ENDED_ORDER_STATUSES


def _exact_record(record, *, project_id, order_id, task_id, run_id, child):
    if type(record) is not dict or not set(ReworkScope.__slots__) <= record.keys():
        _fail("rework_source_unavailable")
    scope = ReworkScope(**{key: record[key] for key in ReworkScope.__slots__})
    scope.validate()
    if ((scope.project_id, scope.order_id, scope.order_task_id, scope.delegation_run_id,
         scope.review_task_id, scope.review_node_id) !=
        (project_id, order_id, task_id, run_id, child.id, child.metadata.get("workflow_node", {}).get("id"))):
        _fail("rework_stale")
    rebuilt = build_pending_record(scope=scope,
        **{key: record.get(key) for key in (
            "expected_attempt", "previous_employee_run_id", "reviewed_artifact_id",
            "reviewed_artifact_version", "reviewed_content_hash", "revision_note",
            "source_refusal_fact_id", "source_review_attempt", "source_review_epoch")})
    if any(record.get(key) != value for key, value in rebuilt.items() if key != "dispatched"):
        _fail("rework_source_unavailable")
    if type(record.get("dispatched")) is not bool:
        _fail("rework_source_unavailable")
    return scope


def _confirm_body(record):
    return {"type": ACTION, "project_id": record["project_id"], "order_id": record["order_id"],
        "task_id": record["order_task_id"], "review_node_id": record["review_node_id"],
        "target_node_id": record["target_node_id"], "rework_record_id": record["record_id"],
        **{key: record[key] for key in ("expected_attempt", "reviewed_artifact_id",
            "reviewed_artifact_version", "reviewed_content_hash", "revision_note", "revision_note_hash")}}


async def read_owner_rework_pending(handler, ws, data):
    reply = {"action": PENDING_ACTION, "ok": False, "effects": False}
    try:
        request = _pending_request(data)
        reply.update(request_id=request["request_id"], project_id=request["project_id"],
                     order_id=request["order_id"], task_id=request["task_id"])
        engine, executor, snapshot, run_id, order, recheck, ended = await _existing_context(handler, ws, request)
        plan, _ = snapshot
        graph, tasks = await _read_graph(executor, plan, run_id)
        graph_by_task = {item.task_id: item for item in graph}
        records = []
        for child in tasks:
            record = child.metadata.get("human_review_rework_to")
            if record is None:
                continue
            scope = _exact_record(record, project_id=request["project_id"], order_id=request["order_id"],
                task_id=request["task_id"], run_id=run_id, child=child)
            if record["dispatched"]:
                continue
            branch = resolve_rework_branch(graph, target_node_id=scope.target_node_id, review_node_id=scope.review_node_id)
            if (branch.review.task_id != child.id or branch.review.work_item_id != scope.review_work_item_id
                    or branch.target.task_id != scope.target_task_id
                    or branch.target.work_item_id != scope.target_work_item_id
                    or graph_by_task[child.id].attempt_seq != record["source_review_attempt"]):
                _fail("rework_stale")
            target = next(item for item in tasks if item.id == scope.target_task_id)
            old = target.metadata.get("employee_run")
            if (type(old) is not dict or old.get("run_id") != record["previous_employee_run_id"]
                    or old.get("attempt") != record["expected_attempt"]):
                _fail("rework_stale")
            await verified_retained_employee_run(executor, target, project_id=scope.project_id,
                order_task_id=scope.order_task_id, target_node_id=scope.target_node_id, order_id=scope.order_id)
            body = _confirm_body(record)
            validate_confirm_request({**body, "request_id": "pending-preview"})
            records.append({"record_id": record["record_id"], "review_task_id": child.id,
                "checkpoint_id": scope.checkpoint_id, "revision": record["record_identity_sha256"],
                "request": body, "allowed_actions": [ACTION] if order.status not in ended else [],
                "consequence": "确认后会重新核对当前权限、原材料版本、次数和额度；通过后，从原节点开始返修。"})
            if len(records) > MAX_PENDING:
                _fail("rework_source_unavailable")
        # Re-read the exact children and original order after bounded collection.
        for view in records:
            child = await executor.store.get_task(view["review_task_id"])
            current = child.metadata.get("human_review_rework_to") if child is not None else None
            if (type(current) is not dict or current.get("record_identity_sha256") != view["revision"]
                    or current.get("dispatched") is not False or _confirm_body(current) != view["request"]):
                _fail("rework_stale")
        current_order = await recheck()
        if current_order.status != order.status:
            _fail("rework_stale")
        return {**reply, "ok": True, "records": records}
    except HumanReviewReworkError as exc:
        return {**reply, "error": exc.code}
    except Exception:
        return {**reply, "error": "rework_source_unavailable"}


async def lookup_owner_rework(handler, ws, data):
    """Observe the original immutable receipt only; never replay its command."""
    reply = {"action": LOOKUP_ACTION, "ok": False, "effects": False}
    try:
        if type(data) is not dict or data.get("type") != LOOKUP_ACTION:
            _fail()
        request = validate_confirm_request({**data, "type": ACTION})
        reply.update(request_id=request["request_id"], project_id=request["project_id"],
                     order_id=request["order_id"], rework_record_id=request["rework_record_id"])
        _, executor, _, run_id, _, recheck, _ = await _existing_context(handler, ws, request)
        receipt = await executor.store.get_human_review_rework_receipt(
            record_id=request["rework_record_id"], request_id=request["request_id"])
        if receipt is None:
            await recheck()
            return {**reply, "ok": True, "status": "not_found"}
        raw_scope, original = receipt.get("scope"), receipt.get("original_record")
        if type(raw_scope) is not dict or set(raw_scope) != set(ReworkScope.__slots__) or type(original) is not dict:
            _fail("rework_source_unavailable")
        scope = ReworkScope(**raw_scope)
        if scope.delegation_run_id != run_id:
            _fail("rework_stale")
        prepared = prepare_confirmation(request=request, record={**original, "dispatched": True}, scope=scope,
            material={key: original[key] for key in ("reviewed_artifact_id", "reviewed_artifact_version", "reviewed_content_hash")},
            current_employee_run_id=original["previous_employee_run_id"], current_employee_attempt=original["expected_attempt"],
            current_review_attempt=original["source_review_attempt"], current_review_epoch=original["source_review_epoch"],
            rework_count=0, max_rework=0, receipt=receipt)
        if not prepared.already_applied:
            _fail("rework_source_unavailable")
        await recheck()
        return {**reply, "ok": True, "status": "already_applied"}
    except HumanReviewReworkError as exc:
        return {**reply, "error": exc.code}
    except Exception:
        return {**reply, "error": "rework_source_unavailable"}
