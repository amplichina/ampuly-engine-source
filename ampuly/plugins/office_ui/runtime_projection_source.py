"""Bounded native capture of a declared workflow source, without installation.

The server selects source_root. It is not a client path or a permission. A
capture is tied to this reader, then rechecked with the real host authority
inside the existing publication transaction. No global org/skill fallback.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
import yaml

from ampuly.layer4_tools.workspace_fs import SecureWorkspace, LexicalWorkspace
from .ai_employee_contract import AIEmployeeContractError, canonical_json
from .ai_employee_safe_files import AIEmployeeSafeFiles
from .runtime_projection import RuntimeProjectionRepository, MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, _grants
from .workflow_packages import WorkflowPackage, project_nodes_onto_org, usable_by_project, staffing_scope_keys


def _fail(code='runtime_source_changed'):
    raise AIEmployeeContractError(code,'业务流原始输入无法一致核对，请重新读取原版本。') from None


def _stamp(fd):
    x=os.fstat(fd)
    if not stat.S_ISREG(x.st_mode) or x.st_nlink!=1:_fail()
    return (x.st_dev,x.st_ino,x.st_size,x.st_mtime_ns,x.st_ctime_ns,x.st_nlink)


@dataclass(frozen=True)
class RuntimeSourceCapture:
    reader: object
    project_id: str
    organization_id: str
    source_revision: str
    source_observation: bytes
    files: object
    grants_json: bytes


class WorkflowRuntimeSource:
    def __init__(self, source_root: Path, package: WorkflowPackage, project_id: str):
        root=Path(os.path.abspath(source_root));pkg=Path(os.path.abspath(package.root))
        if not root.is_absolute() or root==Path(root.anchor) or not pkg.is_relative_to(root):_fail('runtime_source_path_invalid')
        self.root=root;self.package=package;self.project_id=project_id
        self.prefix=pkg.relative_to(root).as_posix()
        if self.prefix=='.':self.prefix=''

    def _relative(self, path):
        path=Path(os.path.abspath(path))
        if not path.is_relative_to(self.root):_fail('runtime_source_path_invalid')
        rel=path.relative_to(self.root).as_posix()
        if not rel or rel=='.' or len(rel)>240 or any(p in {'','..','.'} for p in rel.split('/')):_fail('runtime_source_path_invalid')
        return rel

    def _sweep(self, workspace):
        rows={};stamps={};directories={};total=0;entries=0
        def read(path):
            nonlocal total
            if path in rows:return rows[path]
            if len(rows)>=MAX_FILES:_fail('runtime_source_limit')
            target=workspace.resolve(path,use_output_root=False);fd=workspace.open_read(target)
            try:
                before=_stamp(fd)
                if before[2]>MAX_FILE_BYTES or before[2]+total>MAX_TOTAL_BYTES:_fail('runtime_source_limit')
                data=bytearray()
                while len(data)<=MAX_FILE_BYTES:
                    chunk=os.read(fd,min(64*1024,MAX_FILE_BYTES+1-len(data)))
                    if not chunk:break
                    data.extend(chunk)
                current=workspace.open_read(target)
                try:
                    if len(data)!=before[2] or _stamp(fd)!=before or _stamp(current)!=before:_fail()
                finally:os.close(current)
                rows[path]=bytes(data);stamps[path]=before;total+=len(data)
                return rows[path]
            finally:os.close(fd)
        manifest_path=(self.prefix+'/' if self.prefix else '')+'workflow.yaml'
        manifest_raw=read(manifest_path)
        if self.package.loaded_revision and hashlib.sha256(manifest_raw).hexdigest()!=self.package.loaded_revision:_fail()
        try:manifest=yaml.safe_load(manifest_raw.decode('utf-8'))
        except (ValueError,UnicodeError,yaml.YAMLError):_fail()
        if type(manifest) is not dict or canonical_json(manifest)!=canonical_json(self.package.manifest):_fail()
        if not usable_by_project(WorkflowPackage(self.package.root,manifest),self.project_id):_fail('runtime_source_scope_mismatch')
        if manifest.get('status') not in {'testing','published'}:_fail('runtime_source_not_invokable')
        org_rel=manifest.get('org_config');skills_rel=manifest.get('skills_root','../../skills')
        if type(org_rel) is not str or type(skills_rel) is not str:_fail('runtime_source_path_invalid')
        org=self._relative(self.package.root/org_rel)
        # Only declared package files, the distribution's company_orgs and
        # skills, or this project's explicit skill folder are source families.
        local=self.prefix+'/' if self.prefix else ''
        if not (org.startswith(local) if local else True) and not org.startswith('company_orgs/'):_fail('runtime_source_path_invalid')
        try:org_payload=yaml.safe_load(read(org).decode('utf-8'))
        except (ValueError,UnicodeError,yaml.YAMLError):_fail()
        if type(org_payload) is not dict:_fail()
        skill_names=manifest.get('skills',[])
        if (type(skill_names) is not list or len(skill_names)>64 or len(set(skill_names))!=len(skill_names)
                or any(type(name) is not str or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}',name) for name in skill_names)):_fail()
        skill_root=self._relative(self.package.root/skills_rel)
        allowed=(skill_root=='skills' or skill_root==f'projects/{self.project_id}/skills'
                 or bool(local) and skill_root.startswith(local) or not local)
        if not allowed:_fail('runtime_source_path_invalid')
        resources={};bodies={};versions={};skill_hashes={}
        for skill in skill_names:
            pending=[(skill_root+'/'+skill,0)]
            while pending:
                folder,depth=pending.pop()
                if depth>10 or len(directories)>=128:_fail('runtime_source_limit')
                target=workspace.resolve(folder,use_output_root=False)
                children=(AIEmployeeSafeFiles._list_windows(workspace,target,257-entries) if os.name=='nt'
                          else AIEmployeeSafeFiles._list_posix(workspace,target,257-entries))
                entries+=len(children)
                if entries>256:_fail('runtime_source_limit')
                directories[folder]=children
                for child in children:
                    full=folder+'/'+child['name']
                    if len(full)>240:_fail('runtime_source_limit')
                    if child['kind']=='directory':pending.append((full,depth+1))
                    elif child['kind']=='file':resources['skills/'+full[len(skill_root)+1:]]=read(full)
                    else:_fail()
            entry=resources.get('skills/'+skill+'/SKILL.md')
            if entry is None or not entry:_fail()
            try:
                text=entry.decode('utf-8').replace('\r\n','\n')
                fm=re.match(r'^---\s*\n(.*?)\n---\s*\n',text,flags=re.DOTALL)
                front=yaml.safe_load(fm.group(1)) if fm else {}
                if front is not None and type(front) is not dict:_fail()
                versions[skill]=str((manifest.get('skill_versions',{}) or {}).get(skill) or (front or {}).get('version','') or '')
                skill_hashes[skill]=hashlib.sha256(entry.replace(b'\r\n',b'\n') if b'\x00' not in entry else entry).hexdigest()
                bodies[skill]=re.sub(r'^---\s*\n.*?\n---\s*\n','',text,count=1,flags=re.DOTALL).strip()[:6000]
            except (UnicodeError,yaml.YAMLError):_fail()
        # Every prompt that the frozen org/employee can use is owned now.
        # Reading a later home-relative template is not a valid fallback.
        def prompt(ref):
            if type(ref) is not str:_fail('runtime_prompt_invalid')
            if '\n' not in ref and ' ' not in ref and ref.startswith(('assets/','prompts/')):
                path=self._relative(self.package.root/ref)
                if local and not path.startswith(local):_fail('runtime_source_path_invalid')
                try:return read(path).decode('utf-8')
                except UnicodeError:_fail('runtime_prompt_invalid')
            if '\n' not in ref and (ref.startswith(('/','\\','../','./')) or re.match(r'^[A-Za-z]:',ref)):
                _fail('runtime_prompt_source_required')
            return ref
        for family in ['roles','employees']:
            records=org_payload.get(family,[])
            if type(records) is not list or len(records)>128:_fail()
            for record in records:
                if type(record) is not dict or type(record.get('prompt_refs',[])) is not list:_fail()
                record['prompt_refs']=[prompt(ref) for ref in record.get('prompt_refs',[])]
                if type(record.get('skill_refs',[])) is not list or any(ref not in skill_names for ref in record.get('skill_refs',[])):
                    _fail('runtime_skill_undeclared')
        staffing=copy.deepcopy(manifest.get('staffing',{}) or {})
        if type(staffing) is not dict:_fail()
        from ampuly.core.config import AmpulyConfig,TalentTemplateConfig
        from ampuly.core.org_config import apply_org_config_payload_to_config,validate_runnable_org_config
        templates={}
        def add_template(row):
            template=TalentTemplateConfig.model_validate(row)
            if (not template.id or len(template.id)>128 or template.id!=template.id.strip()
                    or any(ord(c)<32 for c in template.id) or template.id.casefold() in templates):_fail('runtime_template_invalid')
            templates[template.id.casefold()]=template.model_dump(mode='json')
        inline=org_payload.get('talent_templates',[])
        if type(inline) is not list or len(inline)>64:_fail('runtime_template_invalid')
        for raw in inline:
            if type(raw) is not dict:_fail('runtime_template_invalid')
            row=dict(raw);row['prompt_ref']=prompt(row.get('prompt_ref',''));add_template(row)
        sources=staffing.get('template_sources',{})
        if type(sources) is not dict or len(sources)+len(templates)>64:_fail('runtime_template_invalid')
        for template_id,ref in sources.items():
            if type(template_id) is not str or type(ref) is not str or not ref.startswith(('assets/','prompts/')):
                _fail('runtime_template_source_required')
            template_text=prompt(ref)
            fm=re.match(r'^---\s*\n(.*?)\n---\s*\n',template_text,flags=re.DOTALL)
            try:front=yaml.safe_load(fm.group(1)) if fm else {}
            except yaml.YAMLError:_fail('runtime_template_invalid')
            if front is None:front={}
            if type(front) is not dict or front.get('id',template_id)!=template_id:_fail('runtime_template_invalid')
            body=template_text[fm.end():].strip() if fm else template_text.strip()
            row={k:front[k] for k in ['name','description','category','domains','tags','metadata','preferred_external_agent'] if k in front}
            row.update(id=template_id,name=row.get('name') or template_id,prompt_ref=body,
                source_repo='workflow:'+self.package.id,source_path=ref,
                source_revision=hashlib.sha256(read(self._relative(self.package.root/ref))).hexdigest())
            add_template(row)
        selections=staffing.get('staffing_selections',{})
        if type(selections) is not dict:_fail('runtime_staffing_invalid')
        available={row['id'] for row in templates.values()}
        for selection in selections.values():
            if type(selection) is dict:
                kind=selection.get('kind',selection.get('source',''))
                template_id=selection.get('id',selection.get('template_id',''))
            elif type(selection) is str:kind,_,template_id=selection.partition(':')
            else:_fail('runtime_staffing_invalid')
            if kind=='template' and template_id not in available:_fail('runtime_template_source_required')
        projected=project_nodes_onto_org(org_payload,WorkflowPackage(self.package.root,manifest),project_id=self.project_id,skill_bodies=bodies)
        projected['organization_id']=self.package.org_id
        for role in projected.get('roles',[]):
            refs=role.get('skill_refs',[])
            if type(refs) is not list or any(type(ref) is not str or ref not in skill_names for ref in refs):
                _fail('runtime_skill_undeclared')
        compiled=apply_org_config_payload_to_config(AmpulyConfig(),projected)
        validate_runnable_org_config(compiled,organization_id=self.package.org_id)
        if len({role.id for role in compiled.org.roles})!=len(compiled.org.roles):_fail('runtime_org_invalid')
        scopes={key:{'company_profile':key.split('|')[0].split(':')[1],
            'org_id':self.package.org_id if key.startswith('profile:custom|') else '',
            'recruitment_role_agents':staffing.get('recruitment_role_agents',{}),
            'staffing_selections':staffing.get('staffing_selections',{}),'source':'workflow:'+self.package.id}
            for key in staffing_scope_keys(self.package)}
        result={'org/config.json':canonical_json(projected),'staffing/defaults.json':canonical_json({'scopes':scopes}),
                'talent/templates.json':canonical_json({'templates':list(templates.values())}),
                'workflow/manifest.json':canonical_json(manifest),
                'workflow/contract.json':canonical_json({'project_id':self.project_id,'workflow_id':self.package.id,
                    'workflow_version':str(manifest.get('version','')),'skill_versions':versions,'skill_sha256':skill_hashes,
                    'manifest_sha256':hashlib.sha256(manifest_raw.replace(b'\r\n',b'\n') if b'\x00' not in manifest_raw else manifest_raw).hexdigest()}),
                **resources}
        observation=canonical_json({'files':{k:hashlib.sha256(v).hexdigest() for k,v in rows.items()},
            'stamps':{k:list(v) for k,v in stamps.items()},'directories':directories,'package_root':self.prefix})
        return observation,result,_grants(manifest.get('tool_grants',{}) or {})

    def capture(self):
        try:
            with SecureWorkspace.for_roots(str(self.root),str(self.root)) as workspace:
                if isinstance(workspace,LexicalWorkspace):_fail('runtime_source_native_required')
                first=self._sweep(workspace);second=self._sweep(workspace)
                if first!=second:_fail()
                with SecureWorkspace.for_roots(str(self.root),str(self.root)) as current:
                    if os.name=='nt':same=workspace._identity(workspace.root_handle)==current._identity(current.root_handle)
                    else:same=(os.fstat(workspace.root_fd).st_dev,os.fstat(workspace.root_fd).st_ino)==(os.fstat(current.root_fd).st_dev,os.fstat(current.root_fd).st_ino)
                    if not same:_fail()
        except AIEmployeeContractError:raise
        except (OSError,ValueError,TypeError,AttributeError):_fail()
        observation,files,grants=second
        if len(files)>MAX_FILES or sum(map(len,files.values()))>MAX_TOTAL_BYTES:_fail('runtime_source_limit')
        return RuntimeSourceCapture(self,self.project_id,self.package.org_id,hashlib.sha256(observation).hexdigest(),
            observation,MappingProxyType(files),canonical_json(grants))

    def publish(self, repository, capture, *, actor,request_id,expected_revision,assert_authorized):
        if type(capture) is not RuntimeSourceCapture or capture.reader is not self or not callable(assert_authorized):_fail()
        def final_guard():
            # Existing transaction enforces a synchronous completed guard.
            repository.store._before_commit(assert_authorized)
            current=self.capture()
            if current.source_observation!=capture.source_observation or current.files!=capture.files:_fail()
        return repository.publish(project_id=capture.project_id,organization_id=capture.organization_id,
            actor=actor,request_id=request_id,source_revision=capture.source_revision,
            expected_revision=expected_revision,files=dict(capture.files),grant_intents=json.loads(capture.grants_json),
            assert_authorized_and_fresh=final_guard)


class RuntimeProjectionLoader:
    """Read the exact generation already pinned on an original stored Task.

    No client metadata is accepted here, and a reference is not permission.
    Execution leases, Stop, model ceilings and tool authorization stay live.
    """
    def __init__(self,ampuly_home):self.repository=RuntimeProjectionRepository(ampuly_home)

    def __call__(self,task,*,project_id,organization_id):
        from ampuly.core.runtime_inputs import RuntimeInputs,RuntimeInputsError
        try:
            if task.project_id!=project_id or task.org_id!=organization_id:raise ValueError
            contract=task.metadata['workflow_contract']
            if contract.get('project_id')!=project_id:raise ValueError
            projection=self.repository.read_pinned(contract['runtime_projection'],
                project_id=project_id,organization_id=organization_id)
            manifest=json.loads(projection.files['workflow/manifest.json'])
            if (contract.get('workflow_id')!=manifest['id'] or contract.get('workflow_version')!=manifest['version']
                    or manifest.get('org_id',manifest['id'])!=organization_id):raise ValueError
            inputs=RuntimeInputs(project_id,organization_id,projection.reference,projection.files)
            inputs.validate_contract(contract)
            return inputs
        except (ValueError,KeyError,TypeError,AttributeError,AIEmployeeContractError):
            raise RuntimeInputsError() from None
