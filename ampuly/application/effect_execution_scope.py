"""Server-owned pre-dispatch checks; uses existing store, checkpoints and budget reader."""
from __future__ import annotations
import asyncio
import inspect


def _value(value):
    return str(getattr(value, "value", value) or "")


async def capture_server_execution_scope(engine, *, task_id="", session_id=""):
    project = str(engine.project_id)
    task = await engine.store.get_task(task_id) if task_id else None
    session = await engine.store.get_session(session_id) if session_id else None
    anchor = task if task is not None else session
    if anchor is None or str(getattr(anchor, "project_id", "")) != project:
        raise ValueError("effect execution anchor is unavailable or crosses project")
    metadata = dict(getattr(anchor, "metadata", {}) or {})
    return {"project_id": project, "task_id": task_id, "session_id": session_id,
            "budget_id": str(metadata.get("budget_id") or ""),
            "delegation_run_id": str(metadata.get("delegation_run_id") or ""),
            "controller_owner_token": str(metadata.get("company_run_controller_owner_token") or ""),
            "controller_generation": metadata.get("company_run_controller_lease_generation")}


async def check_engine_effect_execution_scope(engine, record):
    from ampuly.application.effect_approval import EffectApprovalService
    from ampuly.layer2_organization.company_mode import run_stopped_by_owner
    store = engine.store
    project = engine.project_id
    home = getattr(engine, "ampuly_home", None)
    db_path, db = getattr(store, "db_path", None), getattr(store, "_db", None)
    gateway = getattr(engine, "effect_gateway", None)
    service = getattr(engine, "effect_approval_service", None)
    journal = getattr(gateway, "journal", None)
    reader = getattr(engine, "effect_budget_reader", None)

    def sources_current():
        return (engine.store is store and engine.project_id == project
                and getattr(engine, "ampuly_home", None) == home
                and getattr(store, "db_path", None) == db_path
                and getattr(store, "_db", None) is db
                and getattr(engine, "effect_gateway", None) is gateway
                and getattr(gateway, "journal", None) is journal
                and getattr(engine, "effect_approval_service", None) is service
                and (service is None or getattr(service, "gateway", None) is gateway)
                and getattr(engine, "effect_budget_reader", None) is reader)

    scope_reader = getattr(journal, "execution_scope", None)
    if not callable(scope_reader):
        return "execution_scope_unavailable"
    # Authority is immutable server metadata on the existing effect_proposed
    # journal event. Provider payload/schema/hash remain untouched.
    scope = await asyncio.to_thread(scope_reader, record.effect_id)
    if not sources_current():
        return "execution_guard_unavailable"
    if not isinstance(scope, dict):
        return "execution_scope_unavailable"
    scope = dict(scope)
    if not scope or scope.get("project_id") != str(project) or record.project_id != str(project):
        return "execution_scope_unavailable"
    if getattr(engine, "_shutting_down", False):
        return "execution_stopped"
    checkpoint = await store.get_execution_checkpoint(
        EffectApprovalService.checkpoint_id_for_effect(record.effect_id),
        project_id=record.project_id, checkpoint_type="action_permission")
    if not sources_current():
        return "execution_guard_unavailable"
    if checkpoint is None:
        return "execution_checkpoint_unavailable"
    try:
        EffectApprovalService._assert_checkpoint_binds_effect(checkpoint, record)
    except ValueError:
        return "execution_checkpoint_mismatch"
    decision = dict(dict((checkpoint.payload or {}).get("interaction") or {}).get("decision") or {})
    decision_hash = str(decision.get("decision_hash") or "")
    if not decision_hash or record.authorization_id != EffectApprovalService.authorization_id(checkpoint.checkpoint_id, decision_hash):
        return "execution_authorization_mismatch"
    if _value(checkpoint.status) != "consuming":
        return "execution_checkpoint_not_consuming"
    if str(checkpoint.task_id or "") != scope.get("task_id", "") or str(checkpoint.session_id or "") != scope.get("session_id", ""):
        return "execution_anchor_mismatch"

    async def current_authority_refusal():
        if not sources_current():
            return "execution_guard_unavailable"
        if getattr(engine, "_shutting_down", False):
            return "execution_stopped"
        task_id, session_id = scope.get("task_id", ""), scope.get("session_id", "")
        task = await store.get_task(task_id) if task_id else None
        if not sources_current():
            return "execution_guard_unavailable"
        session = await store.get_session(session_id) if session_id else None
        if not sources_current():
            return "execution_guard_unavailable"
        scope_anchor = task if task is not None else session
        if scope_anchor is None or str(getattr(scope_anchor, "project_id", "")) != str(project):
            return "execution_guard_unavailable"
        scope_metadata = dict(getattr(scope_anchor, "metadata", {}) or {})
        current = {
            "project_id": str(project), "task_id": task_id, "session_id": session_id,
            "budget_id": str(scope_metadata.get("budget_id") or ""),
            "delegation_run_id": str(scope_metadata.get("delegation_run_id") or ""),
            "controller_owner_token": str(scope_metadata.get("company_run_controller_owner_token") or ""),
            "controller_generation": scope_metadata.get("company_run_controller_lease_generation"),
        }
        if current != scope:
            return "execution_scope_changed"
        anchor = await store.get_task(scope["task_id"]) if scope.get("task_id") else await store.get_session(scope["session_id"])
        if not sources_current():
            return "execution_guard_unavailable"
        metadata = dict(getattr(anchor, "metadata", {}) or {})
        if _value(getattr(anchor, "status", "")) in {"done", "completed", "failed", "cancelled", "canceled", "closed"} or run_stopped_by_owner(metadata):
            return "execution_stopped"
        if scope.get("task_id") and _value(getattr(anchor, "status", "")) not in {
            "pending", "running", "idle", "blocked", "awaiting_peer",
            "awaiting_manager_review", "awaiting_human", "awaiting_review"}:
            return "execution_task_state_unknown"
        if scope.get("delegation_run_id"):
            if not scope.get("controller_owner_token") or scope.get("controller_generation") is None:
                return "execution_controller_unavailable"
            run = await store.get_delegation_run(scope["delegation_run_id"])
            if not sources_current():
                return "execution_guard_unavailable"
            if run is None or str(getattr(run, "project_id", "")) != record.project_id:
                return "execution_run_unavailable"
            if _value(getattr(run, "status", "")) in {"done", "completed", "failed", "cancelled", "canceled", "closed", "unknown", "pending_verification"}:
                return "execution_run_not_active"
            current_lease = await store.delegation_run_controller_lease_is_current(
                scope["delegation_run_id"], project_id=record.project_id,
                owner_token=scope["controller_owner_token"], generation=scope["controller_generation"])
            if not sources_current():
                return "execution_guard_unavailable"
            if not current_lease:
                return "execution_controller_stale"
        if getattr(engine, "_shutting_down", False):
            return "execution_stopped"
        return None

    refusal = await current_authority_refusal()
    if refusal is not None:
        return refusal
    if record.run_id and record.run_id != scope.get("delegation_run_id"):
        return "execution_run_mismatch"
    if not callable(reader) or not scope.get("budget_id"):
        return "execution_budget_unavailable"
    budget = reader(scope["budget_id"])
    if inspect.isawaitable(budget):
        budget = await budget
    if not sources_current():
        return "execution_guard_unavailable"
    # Exactly one fresh authority pass after the budget wait. This closes
    # loss of authority during that wait, not later cross-store revocation
    # or an atomic fence with the Gateway's subsequent dispatch operation.
    refusal = await current_authority_refusal()
    if refusal is not None:
        return refusal
    if not isinstance(budget, dict):
        return "execution_budget_unavailable"
    for field in ("project_id", "customer_id", "environment_id"):
        if str(budget.get(field) or "") != str(getattr(record, field, "") or ""):
            return "execution_budget_scope_mismatch"
    if budget.get("state") != "open" or budget.get("health", "ok") != "ok":
        return "execution_budget_held"
    available = dict(budget.get("available") or {})
    # A browser submit consumes neither an image nor a model-call/retry unit.
    # Those meters are reserved by their own execution paths. An image cap of
    # zero must keep images disabled without blocking an approved page click.
    # The shared monetary stop still applies; other effect kinds keep their
    # existing guard until they declare a separate metering contract.
    if (getattr(record, "kind", "") == "browser.submit"
            and getattr(record, "provider", "") == "browser:submit"):
        available = {"cost_cny": available.get("cost_cny")}
    if any(value is not None and float(value) <= 0 for value in available.values()):
        return "execution_budget_exhausted"
    return None


_TERMINAL_ANCHOR = {"done", "completed", "failed", "cancelled", "canceled", "closed"}
_ACTIVE_TASK = {"pending", "running", "idle", "blocked", "awaiting_peer",
                "awaiting_manager_review", "awaiting_human", "awaiting_review"}
_INACTIVE_RUN = {"done", "completed", "failed", "cancelled", "canceled", "closed", "unknown", "pending_verification"}


def engine_effect_dispatch_fence(engine):
    """Synchronous authority fence for Journal.begin_dispatch (wave 1).

    Runs on the journal's BEGIN IMMEDIATE connection. The runtime Store and the
    effect journal share one SQLite file (Engine.initialize), so these reads
    and the dispatch event are one write-locked transaction: a release, stop,
    Run end or lease change committed after the awaiting guard is seen here.
    It re-derives the same facts as the guard with the same refusal codes.
    After this commit the provider is entered; revoking an in-flight provider
    call is out of scope (cut-off is the dispatch event).
    """
    import json
    import os
    from datetime import datetime
    from ampuly.effects.journal import EffectJournal
    from ampuly.layer2_organization.company_mode import run_stopped_by_owner

    def same_file(left, right):
        try:
            return bool(left) and bool(right) and (
                os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right))))
        except Exception:
            return False

    def fence(connection, record):
        project = str(engine.project_id)
        journal = getattr(getattr(engine, "effect_gateway", None), "journal", None)
        if not same_file(getattr(journal, "db_path", None), getattr(engine.store, "db_path", None)):
            return "execution_guard_unavailable"
        scope = EffectJournal._execution_scope(connection, record.effect_id)
        if not isinstance(scope, dict) or not scope or scope.get("project_id") != project or record.project_id != project:
            return "execution_scope_unavailable"
        if getattr(engine, "_shutting_down", False):
            return "execution_stopped"

        def row(sql, key):
            found = connection.execute(sql, (key,)).fetchall() if key else []
            if len(found) > 1:
                raise ValueError("ambiguous authority row")
            return found[0] if found else None

        task_id, session_id = scope.get("task_id", ""), scope.get("session_id", "")
        task = row("SELECT project_id, status, metadata FROM tasks WHERE id = ?", task_id)
        session = row("SELECT project_id, status, metadata FROM sessions WHERE session_id = ?", session_id)
        anchor = task if task is not None else session
        if anchor is None or str(anchor[0] or "default") != project:
            return "execution_guard_unavailable"
        metadata = json.loads(anchor[2] or "{}")
        if not isinstance(metadata, dict):
            return "execution_guard_unavailable"
        current = {
            "project_id": project, "task_id": task_id, "session_id": session_id,
            "budget_id": str(metadata.get("budget_id") or ""),
            "delegation_run_id": str(metadata.get("delegation_run_id") or ""),
            "controller_owner_token": str(metadata.get("company_run_controller_owner_token") or ""),
            "controller_generation": metadata.get("company_run_controller_lease_generation"),
        }
        if current != scope:
            return "execution_scope_changed"
        status = str(anchor[1] or "")
        if status in _TERMINAL_ANCHOR or run_stopped_by_owner(metadata):
            return "execution_stopped"
        if task_id and status not in _ACTIVE_TASK:
            return "execution_task_state_unknown"
        if scope.get("delegation_run_id"):
            if not scope.get("controller_owner_token") or scope.get("controller_generation") is None:
                return "execution_controller_unavailable"
            run = row(
                "SELECT project_id, status, controller_owner_token, controller_lease_generation, "
                "controller_lease_expires_at FROM delegation_runs WHERE run_id = ?",
                scope["delegation_run_id"])
            if run is None or str(run[0] or "default") != record.project_id:
                return "execution_run_unavailable"
            if str(run[1] or "") in _INACTIVE_RUN:
                return "execution_run_not_active"
            if (not str(run[2] or "") or str(run[2]) != str(scope["controller_owner_token"])
                    or int(run[3] or 0) != int(scope["controller_generation"])
                    or not str(run[4] or "") or str(run[4]) <= datetime.now().isoformat()):
                return "execution_controller_stale"
        if record.run_id and record.run_id != scope.get("delegation_run_id"):
            return "execution_run_mismatch"
        if getattr(engine, "_shutting_down", False):
            return "execution_stopped"
        return None

    return fence


def bind_engine_effect_execution_guard(engine):
    async def guard(record):
        return await check_engine_effect_execution_scope(engine, record)
    engine.effect_gateway.bind_execution_guard(guard)
    # The SQL fence reads the runtime Store's own tables on the journal
    # connection, so it is installed for the real AmpulyStore (always the case
    # in Engine.initialize). A real Store whose file differs from the journal
    # still gets the fence, which then refuses every dispatch. Store/gateway
    # test doubles without those tables keep the awaiting guard only.
    from ampuly.database.store import AmpulyStore
    bind_fence = getattr(engine.effect_gateway, "bind_dispatch_fence", None)
    if callable(bind_fence) and isinstance(getattr(engine, "store", None), AmpulyStore):
        bind_fence(engine_effect_dispatch_fence(engine))
