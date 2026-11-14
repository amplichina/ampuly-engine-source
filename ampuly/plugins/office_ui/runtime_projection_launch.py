"""Formal workflow launch preparation over one immutable, original request.

The caller supplies the existing launch-claim/authority recheck. No tool grant,
configuration file, employee installation or second business ledger is created.
"""
from pathlib import Path
import json
import hashlib

from ampuly.core.runtime_inputs import RuntimeInputs
from .ai_employee_contract import AIEmployeeContractError
from .runtime_projection import RuntimeProjectionRepository
from .runtime_projection_source import WorkflowRuntimeSource
from .workflow_packages import validate_invocation, WorkflowPackage, compose_intake


class RuntimeProjectionLaunchHost:
    def __init__(self,ampuly_home):
        self.home=Path(ampuly_home);self.repository=RuntimeProjectionRepository(self.home)

    def prepare(self,package,fields,*,project_id,request_id,assert_original_claim,
                employee_launch_source=None,employee_runtime_host=None,existing_grants_only=False):
        if type(request_id) is not str or not 0 < len(request_id) <= 512 or not callable(assert_original_claim):
            raise AIEmployeeContractError('runtime_claim_required','运行时发布需要原发起请求。')
        # Existing outbox IDs may contain ':'. Derive one stable internal key;
        # never relax the artifact namespace or change the original claim ID.
        request_id='launch-'+hashlib.sha256(request_id.encode('utf-8')).hexdigest()
        # Claim recheck also applies to inspection/recovery. Neither a stored
        # receipt nor a client-provided generation acts as current authority.
        response=self.repository.inspect_committed_publication(project_id=project_id,
            organization_id=package.org_id,actor='workflow-launch',request_id=request_id,
            assert_authorized=assert_original_claim)
        if response is None:
            if package.exec['mode'] != 'org' or package.exec['company_profile'] != 'custom':
                raise AIEmployeeContractError('runtime_mode_unsupported','不可变业务流仅支持独立组织运行。')
            prepared=validate_invocation(self.home,project_id,package,fields,
                employee_launch_source=employee_launch_source,employee_runtime_host=employee_runtime_host,
                existing_grants_only=existing_grants_only)
            # Only server-discovered workflow catalog roots are passed here.
            # Self-contained packages stay confined to their own directory.
            source_root=package.root.parent.parent if package.root.parent.name=='workflows' else package.root
            reader=WorkflowRuntimeSource(source_root,package,project_id)
            observation=reader.capture()
            if json.loads(observation.files['workflow/contract.json'])!=prepared['workflow_contract']:
                raise AIEmployeeContractError('runtime_source_changed','校验与捕获读取了不同的业务流输入。')
            head=self.repository.read_active(project_id,package.org_id)
            response=reader.publish(self.repository,observation,actor='workflow-launch',request_id=request_id,
                expected_revision=head.head_revision if head else None,assert_authorized=assert_original_claim)
        projection=self.repository.read_pinned(response['reference'],project_id=project_id,organization_id=package.org_id)
        inputs=RuntimeInputs(project_id,package.org_id,projection.reference,projection.files)
        source_manifest=json.loads(inputs.files['workflow/manifest.json'])
        if source_manifest.get('id')!=package.id or source_manifest.get('version')!=package.manifest.get('version'):
            raise AIEmployeeContractError('runtime_source_changed','原运行时版本与当前业务流不一致。')
        frozen_package=WorkflowPackage(package.root,source_manifest)
        prepared={'message':compose_intake(frozen_package,fields),'exec':frozen_package.exec,
            'org_id':inputs.organization_id,'workflow_contract':json.loads(inputs.files['workflow/contract.json']),
            'title':str(dict(source_manifest.get('intake',{}) or {}).get('title','') or '{name}').format(
                name=frozen_package.name,**{f['id']:str(fields.get(f['id'],'') or '') for f in frozen_package.intake_fields}).strip()[:80]}
        prepared['workflow_contract']['runtime_projection']=dict(projection.reference)
        return {**prepared,'runtime_inputs':inputs,'installed':{'runtime_generation':inputs.generation},
                'staffing_scopes':list(json.loads(inputs.files['staffing/defaults.json'])['scopes']),
                'tool_grants':{},'permissions_granted':False,'runtime_activated':False}
