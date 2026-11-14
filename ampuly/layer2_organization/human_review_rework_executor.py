"""Owner-confirmed restart through the existing company controller, not a loop.

The transport must resolve the order's original runtime snapshot and bind an
EmployeeRuntimeHost to the authenticated current socket before calling here.
No new model/tool/worker is called before the atomic rework command commits.
"""
from __future__ import annotations

from datetime import datetime

from .human_review_rework import (
    ReworkScope, ReworkGraphItem, prepare_confirmation, resolve_rework_branch,
    validate_confirm_request, _id, _fail,
)
from .human_review_rework_commit import HumanReworkCommit
from .human_review_employee_basis import verified_retained_employee_run


async def _read_graph(executor, plan, run_id):
    items = list(await executor.store.list_delegation_work_items(run_id) or ())
    if not 2 <= len(items) <= 500:
        _fail("rework_graph_invalid")
    links = await executor.store.get_runtime_links_for_work_items([item.work_item_id for item in items])
    projection_by_id = {}
    for item in items:
        _id(item.work_item_id)
        _id(item.projection_id)
        if item.work_item_id in projection_by_id or item.run_id != run_id:
            _fail("rework_graph_invalid")
        projection_by_id[item.work_item_id] = item.projection_id
    planned = {projection.projection_id: tuple(projection.dependency_projection_ids) for projection in plan.projections}
    if len(planned) != len(plan.projections):
        _fail("rework_graph_invalid")
    graph, tasks = [], []
    for item in items:
        if type(item.metadata) is not dict:
            _fail("rework_graph_invalid")
        task_id = links.get(item.work_item_id)
        _id(task_id)
        task = await executor.store.get_task(task_id)
        if task is None or task.metadata.get("delegation_run_id") != run_id:
            _fail("rework_source_unavailable")
        dependencies = item.metadata.get("dependency_work_item_ids", [])
        if (type(dependencies) is not list
                or any(type(value) is not str or value not in projection_by_id for value in dependencies)
                or len(set(dependencies)) != len(dependencies)):
            _fail("rework_graph_invalid")
        dependency_projections = tuple(projection_by_id[value] for value in dependencies)
        if item.projection_id not in planned or set(planned[item.projection_id]) != set(dependency_projections):
            _fail("rework_graph_invalid")
        graph.append(ReworkGraphItem(item.projection_id, item.work_item_id, task.id,
            item.metadata.get("scope_key", ""), dependency_projections, item.phase.value,
            item.metadata.get("attempt_seq", 0), item.metadata.get("attempt_settled", False),
            item.metadata.get("last_transition_reason", "")))
        tasks.append(task)
    return tuple(graph), tasks


async def restart_human_review_rework(executor, plan, tasks, *, request, host,
                                     schedule_execution=None):
    from .company_mode import CompanyExecutorRunState, GateHarnessDecision
    from ampuly.database.store import CompanyControllerRunLifecycleMutation
    from ampuly.core.company_controller import CompanyRunControllerBusy

    request = validate_confirm_request(request)
    if host is None or not callable(getattr(host, "confirm_rework", None)):
        _fail("rework_source_unavailable")
    run_id = executor._delegation_run_id_for_tasks(tasks)
    _id(run_id)
    # Query a permanent command receipt first. A retry after the next attempt
    # starts must not require the old Task to still be in FAILED.
    receipt = await executor.store.get_human_review_rework_receipt(
        record_id=request["rework_record_id"], request_id=request["request_id"])
    if receipt is not None:
        raw_scope, original = receipt.get("scope"), receipt.get("original_record")
        if type(raw_scope) is not dict or set(raw_scope) != set(ReworkScope.__slots__) or type(original) is not dict:
            _fail("rework_source_unavailable")
        scope = ReworkScope(**raw_scope)
        if scope.delegation_run_id != run_id:
            _fail("rework_stale")
        # Observing an already committed receipt must not acquire a new
        # execution lease or call prepare_turn for the superseded attempt.
        observe = getattr(host, "observe_rework_receipt", None)
        if not callable(observe):
            _fail("rework_source_unavailable")
        await observe(scope, original)
        duplicate = prepare_confirmation(request=request, record={**original, "dispatched": True}, scope=scope,
            material={key: original[key] for key in ("reviewed_artifact_id", "reviewed_artifact_version", "reviewed_content_hash")},
            current_employee_run_id=original["previous_employee_run_id"], current_employee_attempt=original["expected_attempt"],
            current_review_attempt=original["source_review_attempt"], current_review_epoch=original["source_review_epoch"],
            rework_count=0, max_rework=0, receipt=receipt)
        if not duplicate.already_applied:
            _fail("rework_source_unavailable")
        return {"status": "already_applied", "request_id": request["request_id"], "record_id": duplicate.record_id}
    if executor.wake_live_run_dispatcher(run_id):
        raise CompanyRunControllerBusy("human review rework cannot replace a live dispatcher")
    graph, refreshed = await _read_graph(executor, plan, run_id)
    candidates = [item for item in graph if item.node_id == request["review_node_id"]]
    if len(candidates) != 1:
        _fail("rework_graph_invalid")
    by_id = {task.id: task for task in refreshed}
    record = by_id[candidates[0].task_id].metadata.get("human_review_rework_to")
    if type(record) is not dict or not set(ReworkScope.__slots__) <= record.keys():
        _fail("rework_source_unavailable")
    scope = ReworkScope(**{key: record[key] for key in ReworkScope.__slots__})
    if scope.delegation_run_id != run_id:
        _fail("rework_stale")
    branch = resolve_rework_branch(graph, target_node_id=scope.target_node_id, review_node_id=scope.review_node_id)
    admission = await executor.acquire_controller_admission(refreshed,
        adopt_released_task_ids={item.task_id for item in branch.reset})
    if admission is None:
        _fail("rework_source_unavailable")
    try:
        graph, refreshed = await _read_graph(executor, plan, run_id)
        branch = resolve_rework_branch(graph, target_node_id=scope.target_node_id, review_node_id=scope.review_node_id)
        by_id = {task.id: task for task in refreshed}
        by_projection = {executor._projection_id_for_task(task): task for task in refreshed}
        run = await executor.store.get_delegation_run(run_id)
        if run is None or run.status in {"cancelled", "stopped"} or run.lifecycle_status in {"cancelled", "archived"}:
            _fail("rework_stale")
        state = CompanyExecutorRunState(active_plan=plan, active_tasks=refreshed,
            controller_run_id=admission.run_id, controller_project_id=admission.project_id,
            controller_root_session_id=admission.root_session_id, controller_owner_token=admission.owner_token,
            controller_lease_generation=admission.generation, controller_heartbeat=admission.heartbeat)
        token = executor._use_run_state(state)
        try:
            # The real runtime requires a current controller lease during
            # policy checks. It is only installed after admission is acquired.
            # Re-read the child's retained record under that admitted scope.
            record = by_id[scope.review_task_id].metadata.get("human_review_rework_to")
            if type(record) is not dict:
                _fail("rework_source_unavailable")
            authority = await host.confirm_rework(scope, record)
            target = by_id[scope.target_task_id]
            old_run = target.metadata.get("employee_run")
            progress = target.metadata.get("employee_run_progress")
            if type(old_run) is not dict or type(progress) is not dict:
                _fail("rework_source_unavailable")
            restored = await verified_retained_employee_run(executor, target, project_id=scope.project_id,
                order_task_id=scope.order_task_id, target_node_id=scope.target_node_id, order_id=scope.order_id)
            material = {key: record[key] for key in ("reviewed_artifact_id", "reviewed_artifact_version", "reviewed_content_hash")}
            prepared = prepare_confirmation(request=request, record=record, scope=scope, material=material,
                current_employee_run_id=restored.identity.run_id, current_employee_attempt=restored.identity.attempt,
                current_review_attempt=record.get("source_review_attempt"), current_review_epoch=record.get("source_review_epoch"),
                rework_count=progress.get("rework_count"), max_rework=authority["max_rework"])
            commit = HumanReworkCommit(scope, request, branch, host, datetime.now().isoformat(), admission.generation, executor)
            commit.marker = commit.marker_for(prepared, material, authority["actor_login"])
            commit.completed_record = {**record, "dispatched": True, "request_id": prepared.request_id,
                "payload_sha256": prepared.payload_sha256, "dispatched_at": commit.confirmed_at,
                "dispatched_by": authority["actor_login"], "next_attempt": prepared.previous_attempt + 1}
            decision = GateHarnessDecision(action="rework_same_work_item", summary=prepared.note,
                target_projection_id=branch.target.projection_id,
                target_projection_ids=[branch.target.projection_id], blockers=[prepared.note],
                blocker_types=["human_review"], source="human_review", judged_by="owner_confirmed_rework",
                blocker_fingerprint=prepared.payload_sha256)
            result = await executor._gate_harness_initiate_rework_for_controller(
                by_id[scope.review_task_id], decision, by_projection, operation="human_review_rework",
                human_rework_commit=commit,
                run_mutation=CompanyControllerRunLifecycleMutation(
                    expected_statuses=(str(run.status),), expected_lifecycle_statuses=(str(run.lifecycle_status),),
                    expected_current_revision=run.current_revision, expected_updated_at=run.updated_at,
                    status="running", lifecycle_status="active",
                    metadata_updates={"human_review_rework_request_id": prepared.request_id,
                                      "human_review_rework_record_id": prepared.record_id}))
        finally:
            executor._reset_run_state(token)
        if result is None or commit.prepared is None:
            _fail("rework_source_unavailable")
        if commit.prepared.already_applied:
            await executor.release_controller_admission(admission)
            return {"status": "already_applied", "request_id": prepared.request_id, "record_id": prepared.record_id}
        resumed = [await executor.store.get_task(task.id) for task in refreshed]
        if any(task is None for task in resumed):
            _fail("rework_outcome_unknown")
        continuation = executor.execute(plan, resumed, controller_admission=admission,
                                        release_controller_admission_on_exit=True)
        if schedule_execution is None:
            await continuation
        else:
            try:
                schedule_execution(continuation)
            except BaseException:
                continuation.close()
                raise
        return {"status": "confirmed", "request_id": prepared.request_id, "record_id": prepared.record_id,
                "next_attempt": prepared.previous_attempt + 1}
    except BaseException:
        if not admission.released:
            await executor.release_controller_admission(admission)
        raise
