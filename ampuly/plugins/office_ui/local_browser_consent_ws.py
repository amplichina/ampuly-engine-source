"""Owner-only volatile local browser consent, derived from the frozen order."""
from __future__ import annotations
import asyncio,hashlib,inspect,json,math
from datetime import datetime,timedelta,timezone
from urllib.parse import urlsplit
from ampuly.computer_use.local_browser_consent import REGISTRY,local_page,POLICY_VERSION
from ampuly.core.runtime_inputs import RuntimeInputsError
from ampuly.computer_use.ego_engine_binding import EgoEngineRefused

ACTION='local_browser_consent'


async def _resolve_ego_order_execution_scope(engine,order,source_task):
    """Resolve the canonical first runtime Root through retained Store facts.

    The UI/source Task is the consent anchor, not an execution Task. Exact
    project/session rows select candidates only; the original query-only Store
    readers prove their creation chain. No metadata search, SQL or repair runs.
    """
    from ampuly.application.effect_execution_scope import capture_server_execution_scope
    from ampuly.core.models import Task,DelegationRun,DelegationWorkItem
    from ampuly.database.first_run_source_basis import FirstRunSourceBasis
    from ampuly.database.order_root_admission import FirstOwnedRootAdmissionFact
    from ampuly.layer2_organization.first_run_source_observation import FirstRunSourceObservation
    from ampuly.layer2_organization.company_runtime_identity import resolve_company_interaction_ownership

    def require(condition):
        if not condition:raise ValueError('ego_order_execution_source_unavailable')
    def value(state):return str(getattr(state,'value',state) or '')
    def live(state):return value(state) in {'pending','running','idle','blocked','awaiting_peer',
        'awaiting_manager_review','awaiting_human','awaiting_review','active'}
    try:
        project=str(engine.project_id)
        anchor=order.launched_task_id
        require(type(source_task) is Task and source_task.id==anchor and source_task.project_id==project
            and order.project_id==project and value(order.status)=='launched'
            and isinstance(source_task.session_id,str) and bool(source_task.session_id)
            and live(source_task.status) and not getattr(engine,'_shutting_down',False))
        session=source_task.session_id
        runs=await engine.store.list_delegation_runs(project_id=project,session_id=session)
        require(type(runs) is list and len(runs)==1 and type(runs[0]) is DelegationRun)
        run=runs[0]
        require(run.project_id==project and run.session_id==session and live(run.status)
            and value(run.lifecycle_status) in {'active','paused','blocked','awaiting_owner','deliverable'}
            and type(run.controller_owner_token) is str and bool(run.controller_owner_token)
            and type(run.controller_lease_generation) is int and run.controller_lease_generation>0)
        items=await engine.store.list_delegation_work_items(run.run_id)
        require(type(items) is list and len(items)<=1024)
        roots=[item for item in items if type(item) is DelegationWorkItem
            and item.kind=='intake' and item.parent_work_item_id is None]
        require(len(roots)==1 and roots[0].run_id==run.run_id)
        root=roots[0]
        selector=dict(project_id=project,session_id=session,creation_source_task_id=anchor,
            controller_owner_token=run.controller_owner_token,
            controller_lease_generation=run.controller_lease_generation)
        runtime=await engine.store._read_first_root_task_with_origin(root,**selector)
        require(type(runtime) is Task and runtime.id!=anchor and runtime.project_id==project
            and runtime.session_id==session and runtime.parent_session_id==session
            and runtime.linked_work_item_id==root.work_item_id and live(runtime.status))
        observation=FirstRunSourceObservation(project,session,anchor,run.run_id,root.work_item_id,runtime.id)
        basis=await engine.store._read_first_run_source_basis_owned(observation=observation)
        require(type(basis) is FirstRunSourceBasis and basis.observation==observation
            and basis.expected_origin.project_id==project and basis.expected_origin.order_id==order.order_id
            and basis.expected_origin.workflow_id==order.workflow_id)
        admission=await engine.store._read_first_owned_root_admission(project_id=project,
            root_task_binding_fact_id=basis.root_task_binding_fact_id)
        require(type(admission) is FirstOwnedRootAdmissionFact
            and (admission.project_id,admission.session_id,admission.source_task_id,admission.run_id,
                admission.root_work_item_id,admission.runtime_task_id)==
                (project,session,anchor,run.run_id,root.work_item_id,runtime.id)
            and (admission.source_task_fact_id,admission.run_creation_fact_id,
                admission.root_creation_fact_id,admission.root_task_binding_fact_id)==
                (basis.source_task_fact_id,basis.run_creation_fact_id,basis.root_creation_fact_id,
                 basis.root_task_binding_fact_id)
            and admission.claim_attempt_seq==1
            and admission.controller_lease_generation==run.controller_lease_generation
            and admission.controller_owner_sha256==hashlib.sha256(run.controller_owner_token.encode()).hexdigest())
        ownership=await resolve_company_interaction_ownership(engine.store,project,
            waiting_task_id=runtime.id,require_company_identity=True)
        require(ownership.get('ui_anchor_task_id')==anchor
            and ownership.get('company_runtime_session_id')==session)
        scope=await capture_server_execution_scope(engine,task_id=runtime.id)
        require(scope.get('project_id')==project and scope.get('task_id')==runtime.id
            and scope.get('delegation_run_id')==run.run_id
            and scope.get('controller_owner_token')==run.controller_owner_token
            and scope.get('controller_generation')==run.controller_lease_generation
            and type(scope.get('budget_id')) is str and bool(scope['budget_id']))
        expected={'order_id':order.order_id,'budget_id':scope['budget_id'],
            'customer_id':str(order.customer_id or ''),'environment_id':str(order.environment_id or '')}
        for task in (source_task,runtime):
            require(type(task.metadata) is dict and all(str(task.metadata.get(key) or '')==val
                for key,val in expected.items()))
        reader=getattr(engine,'effect_budget_reader',None)
        require(callable(reader))
        async def budget_check():
            budget=reader(scope['budget_id'])
            if inspect.isawaitable(budget):budget=await budget
            require(type(budget) is dict and budget.get('budget_id')==scope['budget_id']
                and budget.get('order_id')==order.order_id and budget.get('project_id')==project
                and budget.get('customer_id','')==expected['customer_id']
                and budget.get('environment_id','')==expected['environment_id']
                and budget.get('state')=='open' and budget.get('health','ok')=='ok')
            available=dict(budget.get('available') or {}).get('cost_cny')
            require(available is None or (type(available) in (int,float)
                and math.isfinite(available) and available>0))
        await budget_check()
        # Re-read through the same strict Store seam after all await points.
        current_run=await engine.store.get_delegation_run(run.run_id)
        require(type(current_run) is DelegationRun and current_run.project_id==project
            and current_run.session_id==session and live(current_run.status)
            and current_run.controller_owner_token==run.controller_owner_token
            and current_run.controller_lease_generation==run.controller_lease_generation)
        require(await engine.store.delegation_run_controller_lease_is_current(run.run_id,
            project_id=project,owner_token=run.controller_owner_token,generation=run.controller_lease_generation))
        current=await engine.store._read_first_root_task_with_origin(root,**selector)
        require(type(current) is Task and current.id==runtime.id and current.linked_work_item_id==root.work_item_id
            and live(current.status) and await capture_server_execution_scope(engine,task_id=current.id)==scope)
        require(await engine.store._read_first_run_source_basis_owned(observation=observation)==basis)
        fresh_source=await engine.store.get_task(anchor)
        require(type(fresh_source) is Task and fresh_source.project_id==project
            and fresh_source.id==anchor and fresh_source.session_id==session and live(fresh_source.status)
            and all(str(fresh_source.metadata.get(key) or '')==val for key,val in expected.items()))
        await budget_check()
        return scope
    except Exception:
        raise ValueError('ego_order_execution_source_unavailable') from None


def frozen_local_paths(manifest):
    result={}
    for node in manifest.get('nodes',[]):
        for component in node.get('components',[]):
            if component.get('kind')!='connector' or component.get('ref')!='browser_universal':continue
            p=component.get('params',{})
            for site in p.get('sites',[]):
                origin,_,_=local_page(site)
                allowed=[]
                for entry in p.get('allowed_paths',[]):
                    parsed=urlsplit('http://'+entry)
                    if parsed.hostname!=urlsplit(origin).hostname:raise ValueError('connector path host mismatch')
                    _,prefix,_=local_page(origin+parsed.path)
                    if prefix=='/':raise ValueError('explicit local business paths required')
                    allowed.append(prefix)
                if not allowed:raise ValueError('connector has no explicit local business paths')
                result.setdefault(origin,set()).update(allowed)
    if not result:raise ValueError('no frozen local browser connector')
    return {origin:tuple(sorted(paths)) for origin,paths in result.items()}


async def handle_local_browser_consent(host,ws,data,*,is_owner,actor):
    pid=host._request_project_id(data)
    async def reply(ok,**extra):await host._send_ack(ws,ok=ok,action=ACTION,project_id=pid,**extra)
    if not is_owner or not isinstance(actor,str) or not actor.strip():
        await reply(False,error='owner_required');return
    try:
        order_id=data.get('order_id')
        if not isinstance(order_id,str) or not order_id or len(order_id)>128:raise ValueError('order_id required')
        engine,pid=await host._engine_for_request(data)
        order=await asyncio.to_thread(host._accounts().business.get_order,order_id,project_id=pid)
        if order is None:raise ValueError('order not found in this project')
        anchor=str(getattr(order,'launched_task_id','') or '')
        if not anchor:raise ValueError('launched order required')
        if data.get('operation')=='revoke':
            ego_binding=getattr(engine,'_ego_engine_binding',None)
            if ego_binding is not None:ego_binding.revoke_order(order_id=order_id)
            REGISTRY.revoke(pid,anchor);await reply(True,order_id=order_id,revoked=True);return
        if data.get('operation')=='inspect':
            grant=REGISTRY.inspect(pid,anchor)
            await reply(True,order_id=order_id,active=bool(grant and not grant.stopped and REGISTRY.clock()<grant.expires),
                used=len(grant.used) if grant else 0,maximum=grant.maximum if grant else 0,policy_version=POLICY_VERSION);return
        if data.get('operation')!='authorize':raise ValueError('authorize or revoke required')
        if str(order.status)!='launched':raise ValueError('active launched order required')
        task=await engine.store.get_task(anchor)
        if task is None or task.project_id!=pid or task.id!=anchor:raise ValueError('original order task unavailable')
        from .runtime_projection_source import RuntimeProjectionLoader
        inputs=RuntimeProjectionLoader(engine.ampuly_home)(task,project_id=pid,organization_id=task.org_id)
        manifest=json.loads(inputs.files['workflow/manifest.json'])
        contract=json.loads(inputs.files['workflow/contract.json'])
        if contract.get('project_id')!=pid:raise ValueError('project-owned frozen workflow required')
        paths=frozen_local_paths(manifest)
        fields=getattr(order,'fields',None)
        if not isinstance(fields,dict) and hasattr(order,'to_payload'):fields=order.to_payload().get('fields')
        batch=(fields or {}).get('batch')
        ego_binding=getattr(engine,'_ego_engine_binding',None)
        backend=data.get('backend','ego' if ego_binding is not None and ego_binding.enabled else 'playwright')
        if backend not in ('playwright','ego'):raise ValueError('unsupported browser backend')
        if backend=='ego' and (ego_binding is None or not ego_binding.enabled):raise ValueError('ego_resource_factory_unavailable')
        if backend=='playwright' and ego_binding is not None and ego_binding.enabled:
            # This project was explicitly launched for Ego. Switching backend
            # requires a deliberate launch change, never a reused consent ID.
            raise ValueError('project_ego_backend_selected')
        # Resolve before issuing any local grant. The source/UI Task carries
        # the consent anchor; only its formally bound runtime Root owns Run.
        ego_scope=await _resolve_ego_order_execution_scope(engine,order,task) if backend=='ego' else None
        grant=REGISTRY.authorize(project=pid,anchor_task=anchor,order_id=order_id,batch=batch,actor=actor,
            paths=paths,request_id=data.get('client_request_id'),ttl_seconds=data.get('ttl_seconds',1800),max_actions=data.get('max_actions',40))
        if backend=='ego':
            try:
                ego_binding.authorize_order(grant=grant,frozen_order=order,
                    run_id=ego_scope['delegation_run_id'],budget_id=ego_scope['budget_id'],
                    frozen_manifest=manifest,frozen_contract=contract)
            except Exception:
                REGISTRY.revoke(pid,anchor)
                raise ValueError('ego_order_binding_unavailable') from None
        await reply(True,order_id=order_id,anchor_task_id=anchor,consent_id=grant.consent_id,backend=backend,
            origins=sorted(paths),paths={k:list(v) for k,v in paths.items()},max_actions=grant.maximum,
            expires_at=(datetime.now(timezone.utc)+timedelta(seconds=max(0,grant.expires-REGISTRY.clock()))).isoformat(),
            transient=True,restores_after_restart=False,policy_version=POLICY_VERSION)
    except RuntimeInputsError:
        await reply(False,error='frozen_runtime_inputs_unavailable')
    except EgoEngineRefused:
        # A revocation may already have withdrawn its grant before a resource
        # cleanup failed. Do not turn that partial state into a success ACK.
        await reply(False,error='ego_control_unavailable')
    except (ValueError,TypeError,KeyError) as exc:
        await reply(False,error=str(exc))
